#![no_std]
//! CodeZard subscriptions: paying for a plan and being subscribed to it, in one transaction.
//!
//! # Why this is a contract and not a row in our database
//!
//! Before this, a subscription was an invoice, a memo, and a poller that noticed the payment
//! and wrote a row. Three steps, each of which can be the one that does not happen: the money
//! arrives and the row is not written, or the row is written and the money never came. Here
//! there is one step. [`Contract::subscribe`] moves the payment and records the period in the
//! same invocation, so either both happened or neither did, and the ledger everybody can read
//! is the one that says which.
//!
//! It also means a subscriber can prove what they bought without asking us. The gateway reads
//! this contract to decide entitlement rather than trusting its own bookkeeping — our database
//! stops being the authority on what somebody paid for and goes back to being what it is good
//! at, which is counting tokens.
//!
//! # What it does not do
//!
//! **It does not pull.** Nothing here can take money from an account that has not signed for
//! it: every payment is a `require_auth` on the subscriber. A renewal is the subscriber
//! calling `subscribe` again, which is a deliberate trade — the alternative is a standing
//! allowance, and an allowance is a thing that keeps working after somebody stops watching it.
//!
//! **It does not hold tokens as an account balance per subscriber.** Payments accumulate in
//! the contract and the admin withdraws them. There is no refund path on purpose: a refund is
//! a decision, and decisions belong to people rather than to code that cannot hear the reason.
//!
//! # Time
//!
//! Periods are in seconds of ledger time. `env.ledger().timestamp()` is the close time of the
//! ledger the invocation lands in — it can lag real time by a few seconds and cannot be
//! influenced by the caller, which is the property that matters for an expiry.

use soroban_sdk::{
    contract, contracterror, contractevent, contractimpl, contracttype, panic_with_error, token,
    Address, Env, Symbol, Vec,
};

/// Storage keys. One enum so that no two of them can ever collide by accident.
#[contracttype]
#[derive(Clone)]
pub enum Key {
    /// Who may add plans and take the money out.
    Admin,
    /// The token payments are made in — the Stellar Asset Contract of XLM, in practice.
    Token,
    /// Every plan id, so the catalogue can be read without knowing what to ask for.
    Plans,
    /// One plan.
    Plan(Symbol),
    /// One subscriber's current period.
    Sub(Address),
}

/// What a plan costs and what it grants.
///
/// `tokens` is CodeZard's own unit and means nothing on chain; it is here so that the thing
/// somebody bought is written down in the same place as the fact that they bought it. The
/// gateway credits that many tokens per period.
#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Plan {
    /// In the token's smallest unit. For XLM that is stroops: 1 XLM = 10_000_000.
    pub price: i128,
    /// How long one period lasts, in seconds.
    pub period: u64,
    /// CodeZard tokens granted per period.
    pub tokens: u64,
    /// Off means it can no longer be bought. Existing periods are untouched — withdrawing a
    /// plan from sale must never cancel what somebody already paid for.
    pub active: bool,
}

/// One subscriber's standing, as the chain knows it.
#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Subscription {
    pub plan: Symbol,
    /// When the CURRENT period started. The gateway keys its token grant on this, so renewing
    /// early cannot be made to grant twice for one period.
    pub started: u64,
    pub expires: u64,
    /// How many periods have been paid for in total. Never resets; it is the receipt.
    pub periods: u32,
}

#[contracterror]
#[derive(Copy, Clone, Debug, Eq, PartialEq, PartialOrd, Ord)]
#[repr(u32)]
pub enum Error {
    AlreadyInitialised = 1,
    NotInitialised = 2,
    NoSuchPlan = 3,
    PlanNotForSale = 4,
    /// A price of zero or less, or a period of zero. A free plan is not sold here: it is
    /// granted off chain, where it costs nothing and needs no proof.
    BadPlan = 5,
    NotSubscribed = 6,
    /// Withdrawing more than the contract holds, or an amount that is not positive.
    BadAmount = 7,
}

// ── what this contract says out loud ─────────────────────────────────────────
//
// Emitted so that a subscription can be followed without asking us anything: an indexer, a
// wallet or the subscriber themselves can watch the ledger and see the same facts the gateway
// reads. `#[topic]` marks the fields worth filtering on.

#[contractevent]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Subscribed {
    #[topic]
    pub subscriber: Address,
    #[topic]
    pub plan: Symbol,
    pub expires: u64,
    pub price: i128,
    pub periods: u32,
}

#[contractevent]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlanSet {
    #[topic]
    pub plan: Symbol,
    pub price: i128,
    pub period: u64,
    pub tokens: u64,
    pub active: bool,
}

#[contractevent]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Withdrawn {
    #[topic]
    pub to: Address,
    pub amount: i128,
}

#[contractevent]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AdminChanged {
    #[topic]
    pub admin: Address,
}

#[contract]
pub struct Contract;

#[contractimpl]
impl Contract {
    /// Set the admin and the token, once.
    ///
    /// Refusing the second call is the whole point: an `initialise` that can be called again
    /// is a back door to replacing the admin, and it is the most common way a contract like
    /// this is taken over.
    pub fn initialise(env: Env, admin: Address, token: Address) {
        if env.storage().instance().has(&Key::Admin) {
            panic_with_error!(&env, Error::AlreadyInitialised);
        }
        env.storage().instance().set(&Key::Admin, &admin);
        env.storage().instance().set(&Key::Token, &token);
        env.storage().instance().set(&Key::Plans, &Vec::<Symbol>::new(&env));
    }

    /// Add a plan or change one. Admin only.
    ///
    /// Changing a plan does NOT touch anybody's current period: what they paid for is what
    /// they keep until it expires. The new price applies from their next `subscribe`.
    pub fn set_plan(env: Env, id: Symbol, price: i128, period: u64, tokens: u64, active: bool) {
        Self::admin(&env).require_auth();
        if price <= 0 || period == 0 {
            panic_with_error!(&env, Error::BadPlan);
        }
        let plan = Plan { price, period, tokens, active };
        env.storage().persistent().set(&Key::Plan(id.clone()), &plan);

        let mut ids: Vec<Symbol> = env
            .storage()
            .instance()
            .get(&Key::Plans)
            .unwrap_or_else(|| Vec::new(&env));
        if !ids.contains(&id) {
            ids.push_back(id.clone());
            env.storage().instance().set(&Key::Plans, &ids);
        }
        PlanSet { plan: id, price, period, tokens, active }.publish(&env);
    }

    /// Pay for a period, and have it recorded. The two are one transaction.
    ///
    /// Subscribing while still inside a period EXTENDS it from where it ends, not from now —
    /// renewing a day early must not cost somebody the rest of the week they already paid for.
    /// `started` only moves when a period had actually lapsed, which is what the gateway keys
    /// its token grant on.
    pub fn subscribe(env: Env, subscriber: Address, id: Symbol) -> Subscription {
        subscriber.require_auth();
        let plan = Self::require_plan(&env, &id);
        if !plan.active {
            panic_with_error!(&env, Error::PlanNotForSale);
        }

        // The payment first. If it fails the whole invocation reverts and nothing below it
        // ever happened — which is the property this contract exists to have.
        let token = token::Client::new(&env, &Self::token(&env));
        token.transfer(&subscriber, &env.current_contract_address(), &plan.price);

        let now = env.ledger().timestamp();
        let current: Option<Subscription> =
            env.storage().persistent().get(&Key::Sub(subscriber.clone()));
        let updated = match current {
            // Same plan, still running: add a period to the end of it.
            Some(existing) if existing.plan == id && existing.expires > now => Subscription {
                plan: id.clone(),
                started: existing.started,
                expires: existing.expires + plan.period,
                periods: existing.periods + 1,
            },
            // Lapsed, or a different plan: a new period starting now. Switching plans does
            // not carry the old one's remaining time across — it was paid for at a different
            // price, and converting between the two is a pricing decision, not arithmetic.
            Some(existing) => Subscription {
                plan: id.clone(),
                started: now,
                expires: now + plan.period,
                periods: existing.periods + 1,
            },
            None => Subscription {
                plan: id.clone(),
                started: now,
                expires: now + plan.period,
                periods: 1,
            },
        };
        env.storage()
            .persistent()
            .set(&Key::Sub(subscriber.clone()), &updated);
        Subscribed {
            subscriber,
            plan: id,
            expires: updated.expires,
            price: plan.price,
            periods: updated.periods,
        }
        .publish(&env);
        updated
    }

    /// What the chain says about one subscriber. `None` when they never subscribed.
    ///
    /// An EXPIRED subscription is still returned, deliberately: "it ran out last Tuesday" and
    /// "you never had one" are different answers, and a screen that cannot tell them apart
    /// cannot offer the right thing to do next.
    pub fn subscription(env: Env, who: Address) -> Option<Subscription> {
        env.storage().persistent().get(&Key::Sub(who))
    }

    /// The one question entitlement actually asks.
    pub fn is_active(env: Env, who: Address) -> bool {
        match env
            .storage()
            .persistent()
            .get::<Key, Subscription>(&Key::Sub(who))
        {
            Some(subscription) => subscription.expires > env.ledger().timestamp(),
            None => false,
        }
    }

    pub fn plan(env: Env, id: Symbol) -> Option<Plan> {
        env.storage().persistent().get(&Key::Plan(id))
    }

    /// Every plan id, so the catalogue can be read without being told what to look for.
    pub fn plans(env: Env) -> Vec<Symbol> {
        env.storage()
            .instance()
            .get(&Key::Plans)
            .unwrap_or_else(|| Vec::new(&env))
    }

    /// What the contract is holding. Public: it is on the ledger either way.
    pub fn balance(env: Env) -> i128 {
        token::Client::new(&env, &Self::token(&env)).balance(&env.current_contract_address())
    }

    /// Take the takings out. Admin only.
    ///
    /// Deliberately NOT "withdraw everything": naming the amount makes the transaction say
    /// what it does, and a signer approving it can read the number before it is moved.
    pub fn withdraw(env: Env, to: Address, amount: i128) {
        Self::admin(&env).require_auth();
        let token = token::Client::new(&env, &Self::token(&env));
        let held = token.balance(&env.current_contract_address());
        if amount <= 0 || amount > held {
            panic_with_error!(&env, Error::BadAmount);
        }
        token.transfer(&env.current_contract_address(), &to, &amount);
        Withdrawn { to, amount }.publish(&env);
    }

    /// Hand the contract to somebody else. Both sign: the outgoing admin authorises the move
    /// and the incoming one proves the address is theirs, so a typo cannot lock it forever.
    pub fn set_admin(env: Env, new_admin: Address) {
        Self::admin(&env).require_auth();
        new_admin.require_auth();
        env.storage().instance().set(&Key::Admin, &new_admin);
        AdminChanged { admin: new_admin }.publish(&env);
    }

    pub fn admin_address(env: Env) -> Address {
        Self::admin(&env)
    }

    pub fn token_address(env: Env) -> Address {
        Self::token(&env)
    }

    // ── internals ────────────────────────────────────────────────────────────

    fn admin(env: &Env) -> Address {
        env.storage()
            .instance()
            .get(&Key::Admin)
            .unwrap_or_else(|| panic_with_error!(env, Error::NotInitialised))
    }

    fn token(env: &Env) -> Address {
        env.storage()
            .instance()
            .get(&Key::Token)
            .unwrap_or_else(|| panic_with_error!(env, Error::NotInitialised))
    }

    fn require_plan(env: &Env, id: &Symbol) -> Plan {
        env.storage()
            .persistent()
            .get(&Key::Plan(id.clone()))
            .unwrap_or_else(|| panic_with_error!(env, Error::NoSuchPlan))
    }
}

#[cfg(test)]
mod test;

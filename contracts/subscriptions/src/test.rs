//! What the contract has to be true about, checked against a real ledger in the test host.
//!
//! The interesting cases are all about money and time: that a payment and a record cannot
//! come apart, that renewing early does not cost somebody the rest of what they paid for, and
//! that nobody but the admin can move anything.

#![cfg(test)]

use soroban_sdk::testutils::{Address as _, Ledger as _};
use soroban_sdk::token::{StellarAssetClient, TokenClient};
use soroban_sdk::{symbol_short, Address, Env, Symbol};

use crate::{Contract, ContractClient, Error, Plan};

const WEEK: u64 = 7 * 24 * 60 * 60;
const MONTH: u64 = 30 * 24 * 60 * 60;
/// 19 XLM in stroops. Prices on chain are in the token's smallest unit, always.
const PRICE: i128 = 19 * 10_000_000;

struct World<'a> {
    env: Env,
    contract: ContractClient<'a>,
    token: TokenClient<'a>,
    mint: StellarAssetClient<'a>,
    admin: Address,
}

fn world<'a>() -> World<'a> {
    let env = Env::default();
    env.mock_all_auths();

    let admin = Address::generate(&env);
    // A Stellar Asset Contract, which is exactly what native XLM is on Soroban — so the
    // tests exercise the same token interface production does.
    let asset = env.register_stellar_asset_contract_v2(admin.clone());
    let token = TokenClient::new(&env, &asset.address());
    let mint = StellarAssetClient::new(&env, &asset.address());

    let id = env.register(Contract, ());
    let contract = ContractClient::new(&env, &id);
    contract.initialise(&admin, &asset.address());

    World { env, contract, token, mint, admin }
}

impl World<'_> {
    fn subscriber(&self, funds: i128) -> Address {
        let who = Address::generate(&self.env);
        self.mint.mint(&who, &funds);
        who
    }

    fn starter(&self) {
        self.contract
            .set_plan(&symbol_short!("starter"), &PRICE, &MONTH, &4_000_000, &true);
    }

    fn advance(&self, seconds: u64) {
        self.env.ledger().with_mut(|l| l.timestamp += seconds);
    }
}

// ── setting up ───────────────────────────────────────────────────────────────

#[test]
fn it_remembers_its_admin_and_its_token() {
    let world = world();
    assert_eq!(world.contract.admin_address(), world.admin);
    assert_eq!(world.contract.token_address(), world.token.address);
}

#[test]
fn it_can_only_be_initialised_once() {
    // An `initialise` that can be called again is a back door to replacing the admin.
    let world = world();
    let attacker = Address::generate(&world.env);
    let result = world
        .contract
        .try_initialise(&attacker, &world.token.address);
    assert_eq!(result, Err(Ok(Error::AlreadyInitialised.into())));
    assert_eq!(world.contract.admin_address(), world.admin);
}

#[test]
fn a_plan_can_be_added_and_read_back() {
    let world = world();
    world.starter();

    let plan = world.contract.plan(&symbol_short!("starter")).unwrap();
    assert_eq!(
        plan,
        Plan { price: PRICE, period: MONTH, tokens: 4_000_000, active: true }
    );
    assert_eq!(world.contract.plans().len(), 1);
}

#[test]
fn the_catalogue_lists_each_plan_once_however_often_it_is_edited() {
    let world = world();
    world.starter();
    world.starter();
    world
        .contract
        .set_plan(&symbol_short!("studio"), &PRICE, &MONTH, &9, &true);

    assert_eq!(world.contract.plans().len(), 2);
}

#[test]
fn a_plan_that_costs_nothing_is_refused() {
    // Free is granted off chain, where it costs nothing and needs no proof. A zero-price plan
    // here would be a subscription anybody could mint.
    let world = world();
    let result = world
        .contract
        .try_set_plan(&symbol_short!("free"), &0, &WEEK, &1, &true);
    assert_eq!(result, Err(Ok(Error::BadPlan.into())));
}

#[test]
fn a_plan_with_no_period_is_refused() {
    let world = world();
    let result = world
        .contract
        .try_set_plan(&symbol_short!("broken"), &PRICE, &0, &1, &true);
    assert_eq!(result, Err(Ok(Error::BadPlan.into())));
}

// ── subscribing ──────────────────────────────────────────────────────────────

#[test]
fn subscribing_moves_the_money_and_records_the_period_together() {
    let world = world();
    world.starter();
    let who = world.subscriber(PRICE * 2);

    let subscription = world.contract.subscribe(&who, &symbol_short!("starter"));

    assert_eq!(world.token.balance(&who), PRICE);
    assert_eq!(world.contract.balance(), PRICE);
    assert_eq!(subscription.plan, symbol_short!("starter"));
    assert_eq!(subscription.periods, 1);
    assert_eq!(subscription.expires, subscription.started + MONTH);
    assert!(world.contract.is_active(&who));
}

#[test]
fn without_the_money_nothing_is_recorded_either() {
    // The whole reason this is a contract: the payment and the record cannot come apart.
    let world = world();
    world.starter();
    let broke = world.subscriber(PRICE - 1);

    assert!(world
        .contract
        .try_subscribe(&broke, &symbol_short!("starter"))
        .is_err());

    assert_eq!(world.contract.subscription(&broke), None);
    assert_eq!(world.contract.balance(), 0);
    assert!(!world.contract.is_active(&broke));
}

#[test]
fn an_unknown_plan_is_refused_before_any_money_moves() {
    let world = world();
    let who = world.subscriber(PRICE);

    let result = world.contract.try_subscribe(&who, &symbol_short!("nope"));

    assert_eq!(result, Err(Ok(Error::NoSuchPlan.into())));
    assert_eq!(world.token.balance(&who), PRICE);
}

#[test]
fn a_plan_withdrawn_from_sale_cannot_be_bought() {
    let world = world();
    world.starter();
    world
        .contract
        .set_plan(&symbol_short!("starter"), &PRICE, &MONTH, &4_000_000, &false);
    let who = world.subscriber(PRICE);

    let result = world.contract.try_subscribe(&who, &symbol_short!("starter"));

    assert_eq!(result, Err(Ok(Error::PlanNotForSale.into())));
    assert_eq!(world.token.balance(&who), PRICE);
}

#[test]
fn withdrawing_a_plan_from_sale_does_not_cancel_what_was_paid_for() {
    let world = world();
    world.starter();
    let who = world.subscriber(PRICE);
    world.contract.subscribe(&who, &symbol_short!("starter"));

    world
        .contract
        .set_plan(&symbol_short!("starter"), &PRICE, &MONTH, &4_000_000, &false);

    assert!(world.contract.is_active(&who));
}

// ── time ─────────────────────────────────────────────────────────────────────

#[test]
fn it_expires_when_the_period_runs_out() {
    let world = world();
    world.starter();
    let who = world.subscriber(PRICE);
    world.contract.subscribe(&who, &symbol_short!("starter"));

    world.advance(MONTH - 1);
    assert!(world.contract.is_active(&who));
    world.advance(2);
    assert!(!world.contract.is_active(&who));
}

#[test]
fn an_expired_subscription_is_still_readable() {
    // "It ran out last Tuesday" and "you never had one" are different answers, and a screen
    // that cannot tell them apart cannot offer the right thing to do next.
    let world = world();
    world.starter();
    let who = world.subscriber(PRICE);
    world.contract.subscribe(&who, &symbol_short!("starter"));
    world.advance(MONTH + 1);

    assert!(!world.contract.is_active(&who));
    assert!(world.contract.subscription(&who).is_some());
}

#[test]
fn renewing_early_adds_to_the_end_rather_than_restarting() {
    // Renewing a day early must not cost somebody the rest of the month they already paid for.
    let world = world();
    world.starter();
    let who = world.subscriber(PRICE * 2);
    let first = world.contract.subscribe(&who, &symbol_short!("starter"));

    world.advance(MONTH - 1);
    let second = world.contract.subscribe(&who, &symbol_short!("starter"));

    assert_eq!(second.expires, first.expires + MONTH);
    assert_eq!(second.started, first.started, "still the same period it began in");
    assert_eq!(second.periods, 2);
}

#[test]
fn renewing_after_it_lapsed_starts_a_fresh_period() {
    let world = world();
    world.starter();
    let who = world.subscriber(PRICE * 2);
    let first = world.contract.subscribe(&who, &symbol_short!("starter"));
    world.advance(MONTH * 2);

    let second = world.contract.subscribe(&who, &symbol_short!("starter"));

    assert!(second.started > first.expires);
    assert_eq!(second.expires, second.started + MONTH);
    assert_eq!(second.periods, 2, "the receipt keeps counting");
}

#[test]
fn switching_plans_starts_the_new_one_now() {
    // The old period was bought at a different price; converting what is left of it into the
    // new plan is a pricing decision, not arithmetic.
    let world = world();
    world.starter();
    world
        .contract
        .set_plan(&symbol_short!("studio"), &(PRICE * 2), &WEEK, &40_000_000, &true);
    let who = world.subscriber(PRICE * 5);
    world.contract.subscribe(&who, &symbol_short!("starter"));
    world.advance(10);

    let switched = world.contract.subscribe(&who, &symbol_short!("studio"));

    assert_eq!(switched.plan, symbol_short!("studio"));
    assert_eq!(switched.expires, switched.started + WEEK);
}

#[test]
fn somebody_who_never_subscribed_has_nothing() {
    let world = world();
    let stranger = Address::generate(&world.env);

    assert_eq!(world.contract.subscription(&stranger), None);
    assert!(!world.contract.is_active(&stranger));
}

// ── the money ────────────────────────────────────────────────────────────────

#[test]
fn the_admin_can_take_the_takings_out() {
    let world = world();
    world.starter();
    let who = world.subscriber(PRICE);
    world.contract.subscribe(&who, &symbol_short!("starter"));

    world.contract.withdraw(&world.admin, &PRICE);

    assert_eq!(world.token.balance(&world.admin), PRICE);
    assert_eq!(world.contract.balance(), 0);
}

#[test]
fn more_than_it_holds_cannot_be_withdrawn() {
    let world = world();
    world.starter();
    let who = world.subscriber(PRICE);
    world.contract.subscribe(&who, &symbol_short!("starter"));

    let result = world.contract.try_withdraw(&world.admin, &(PRICE + 1));

    assert_eq!(result, Err(Ok(Error::BadAmount.into())));
    assert_eq!(world.contract.balance(), PRICE);
}

#[test]
fn a_withdrawal_of_nothing_is_refused() {
    let world = world();
    let result = world.contract.try_withdraw(&world.admin, &0);
    assert_eq!(result, Err(Ok(Error::BadAmount.into())));
}

#[test]
fn the_contract_can_be_handed_over() {
    let world = world();
    let next = Address::generate(&world.env);

    world.contract.set_admin(&next);

    assert_eq!(world.contract.admin_address(), next);
}

// ── who may do what ──────────────────────────────────────────────────────────

/// Without `mock_all_auths`, an unsigned call is refused by the host itself. These are the
/// tests that prove the `require_auth` calls are actually there — with mocking on, every one
/// of them would pass whether or not the contract asked for authorisation at all.
mod authorisation {
    use super::*;

    fn unmocked<'a>() -> World<'a> {
        let world = world();
        world.starter();
        let subscriber = world.subscriber(PRICE * 4);
        // Fund the admin too, so a failed withdrawal is about authorisation and nothing else.
        world.mint.mint(&world.admin, &PRICE);
        world.env.set_auths(&[]);
        let _ = subscriber;
        world
    }

    #[test]
    #[should_panic]
    fn nobody_can_subscribe_without_signing_for_it() {
        let world = world();
        world.starter();
        let who = world.subscriber(PRICE);
        world.env.set_auths(&[]);

        world.contract.subscribe(&who, &symbol_short!("starter"));
    }

    #[test]
    #[should_panic]
    fn a_stranger_cannot_add_a_plan() {
        let world = unmocked();
        world
            .contract
            .set_plan(&symbol_short!("gift"), &1, &WEEK, &999_999_999, &true);
    }

    #[test]
    #[should_panic]
    fn a_stranger_cannot_withdraw() {
        let world = unmocked();
        world.contract.withdraw(&Address::generate(&world.env), &1);
    }

    #[test]
    #[should_panic]
    fn a_stranger_cannot_take_over_as_admin() {
        let world = unmocked();
        world.contract.set_admin(&Address::generate(&world.env));
    }
}

/// `Symbol`s are the plan ids, and they travel between the gateway, the browser and here.
/// Anything longer than nine characters needs a different constructor, so the ids in the
/// catalogue have to stay short — this is the test that says so out loud.
#[test]
fn every_shipped_plan_id_fits_in_a_short_symbol() {
    let env = Env::default();
    for id in ["starter", "builder", "studio"] {
        assert!(id.len() <= 9, "{id} is too long for symbol_short!");
        let _ = Symbol::new(&env, id);
    }
}

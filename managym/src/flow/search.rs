// search.rs
// Determinized-search primitives on Game: hidden-information resampling and
// uniformly-random playouts to terminal. Used by flat Monte Carlo search.

use std::collections::{BTreeMap, BTreeSet};

use rand::{seq::SliceRandom, Rng, SeedableRng};
use rand_chacha::ChaCha8Rng;

use crate::{
    agent::action::{ActionSpace, ActionSpaceKind, AgentError},
    flow::game::Game,
    state::{card::CardDefId, game_object::PlayerId, zone::ZoneType},
};

pub type HiddenPoolSummary = (Vec<(CardDefId, usize)>, usize, usize);

/// SplitMix64-style mix of two u64s into a stream-independent sub-seed.
pub fn mix_seed(a: u64, b: u64) -> u64 {
    let mut z = a
        .wrapping_mul(0x9E37_79B9_7F4A_7C15)
        .wrapping_add(b)
        .wrapping_add(0x9E37_79B9_7F4A_7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

impl Game {
    /// Replace the game RNG with a fresh seeded stream (for reproducible
    /// playouts from cloned states).
    pub fn reseed(&mut self, seed: u64) {
        self.state.rng = ChaCha8Rng::seed_from_u64(seed);
    }

    /// Sample one world consistent with `perspective`'s observation.
    ///
    /// Hidden information in this engine is exactly: the opponent's hand, and
    /// the order of both libraries (decklists are known to both players; all
    /// other zones — battlefield, graveyard, stack, exile, command — are
    /// public). Accordingly:
    /// - the opponent's hand is replaced by a uniform sample of |hand| cards
    ///   from their unseen pool (hand ∪ library), with the remainder becoming
    ///   their shuffled library;
    /// - `perspective`'s own library is reshuffled (its order is unknown to
    ///   them, but its contents are determined by the public zones + hand).
    ///
    /// All public state — including the current action space — is preserved.
    pub fn determinize(&mut self, perspective: PlayerId, seed: u64) {
        let mut rng = ChaCha8Rng::seed_from_u64(seed);
        let opponent = PlayerId((perspective.0 + 1) % 2);
        self.journal_zones();
        self.state.zones.resample_hidden(opponent, &mut rng);
        self.state
            .zones
            .shuffle_canonical(ZoneType::Library, perspective, &mut rng);

        self.repin_revealed_library_cards();
    }

    /// Install one exact opponent hand consistent with `perspective`'s
    /// viewer-safe hidden pool, then randomize only the remaining unknown
    /// library order.
    ///
    /// When the installed player owns a priority prompt, its legal actions
    /// are recomputed from the hypothetical hand. Search roots normally have
    /// the perspective player acting and therefore retain their root offers.
    /// Other acting-player prompt kinds fail closed because their candidates
    /// may also depend on private hand state.
    pub fn determinize_to_hand(
        &mut self,
        perspective: PlayerId,
        hand: &[(CardDefId, usize)],
        seed: u64,
    ) -> Result<(), AgentError> {
        if perspective.0 >= self.state.players.len() {
            return Err(AgentError(format!(
                "determinize_to_hand: perspective {} out of range",
                perspective.0
            )));
        }
        let opponent = PlayerId((perspective.0 + 1) % 2);
        let hand_size = self.state.zones.size(ZoneType::Hand, opponent);
        let requested_size = hand.iter().map(|(_, count)| *count).sum::<usize>();
        if requested_size != hand_size {
            return Err(AgentError(format!(
                "determinize_to_hand: requested {requested_size} cards, expected {hand_size}"
            )));
        }
        let mut requested = BTreeMap::<CardDefId, usize>::new();
        for (definition, count) in hand {
            if *count == 0 {
                return Err(AgentError(format!(
                    "determinize_to_hand: definition {} has a zero count",
                    definition.0
                )));
            }
            if requested.insert(*definition, *count).is_some() {
                return Err(AgentError(format!(
                    "determinize_to_hand: duplicate definition {}",
                    definition.0
                )));
            }
        }
        if let Some(space) = self.current_action_space.as_ref() {
            if space.player == Some(opponent) && space.kind != ActionSpaceKind::Priority {
                return Err(AgentError(format!(
                    "determinize_to_hand: cannot refresh acting {:?} prompt",
                    space.kind
                )));
            }
        }

        let pool = self
            .state
            .zones
            .zone_cards(ZoneType::Hand, opponent)
            .iter()
            .chain(
                self.state
                    .zones
                    .zone_cards(ZoneType::Library, opponent)
                    .iter(),
            )
            .copied()
            .collect::<Vec<_>>();
        let mut by_definition = BTreeMap::<CardDefId, Vec<_>>::new();
        for card in pool {
            by_definition
                .entry(self.state.cards[card].definition_id)
                .or_default()
                .push(card);
        }
        let available_definitions = by_definition.keys().copied().collect::<BTreeSet<_>>();
        let mut rng = ChaCha8Rng::seed_from_u64(seed);
        let mut selected = Vec::with_capacity(hand_size);
        let pool_size = by_definition.values().map(Vec::len).sum::<usize>();
        let mut remaining = Vec::with_capacity(pool_size.saturating_sub(hand_size));
        for (definition, mut cards) in by_definition {
            cards.sort_unstable_by_key(|card| card.0);
            cards.shuffle(&mut rng);
            let requested_count = requested.get(&definition).copied().unwrap_or(0);
            if requested_count > cards.len() {
                return Err(AgentError(format!(
                    "determinize_to_hand: requested {requested_count} copies of definition {}, only {} available",
                    definition.0,
                    cards.len()
                )));
            }
            let rest = cards.split_off(requested_count);
            selected.extend(cards);
            remaining.extend(rest);
        }
        for (definition, count) in &requested {
            if !available_definitions.contains(definition) {
                return Err(AgentError(format!(
                    "determinize_to_hand: requested {} copies of definition {}, only 0 available",
                    count, definition.0
                )));
            }
        }

        selected.shuffle(&mut rng);
        remaining.shuffle(&mut rng);
        self.journal_zones();
        self.state
            .zones
            .reassign_hidden(opponent, selected, remaining);
        self.state
            .zones
            .shuffle_canonical(ZoneType::Library, perspective, &mut rng);
        self.repin_revealed_library_cards();

        if self
            .current_action_space
            .as_ref()
            .is_some_and(|space| space.player == Some(opponent))
        {
            self.refresh_priority_actions(opponent);
        }
        Ok(())
    }

    /// Viewer-safe counts for the opponent's current hand-plus-library pool.
    pub fn hidden_pool_summary(
        &self,
        perspective: PlayerId,
    ) -> Result<HiddenPoolSummary, AgentError> {
        if perspective.0 >= self.state.players.len() {
            return Err(AgentError(format!(
                "hidden_pool_summary: perspective {} out of range",
                perspective.0
            )));
        }
        let opponent = PlayerId((perspective.0 + 1) % 2);
        let mut counts = BTreeMap::<CardDefId, usize>::new();
        for card in self
            .state
            .zones
            .zone_cards(ZoneType::Hand, opponent)
            .iter()
            .chain(
                self.state
                    .zones
                    .zone_cards(ZoneType::Library, opponent)
                    .iter(),
            )
        {
            *counts
                .entry(self.state.cards[*card].definition_id)
                .or_default() += 1;
        }
        Ok((
            counts.into_iter().collect(),
            self.state.zones.size(ZoneType::Hand, opponent),
            self.state.zones.size(ZoneType::Library, opponent),
        ))
    }

    pub(crate) fn opponent_hand_definition_ids(&self, perspective: PlayerId) -> Vec<CardDefId> {
        let opponent = PlayerId((perspective.0 + 1) % 2);
        let mut definitions = self
            .state
            .zones
            .zone_cards(ZoneType::Hand, opponent)
            .iter()
            .map(|card| self.state.cards[*card].definition_id)
            .collect::<Vec<_>>();
        definitions.sort_unstable();
        definitions
    }

    fn refresh_priority_actions(&mut self, player: PlayerId) {
        self.invalidate_mana_cache(PlayerId(0));
        self.invalidate_mana_cache(PlayerId(1));
        let actions = self.compute_player_actions(player);
        self.publish_action_space(ActionSpace {
            player: Some(player),
            kind: ActionSpaceKind::Priority,
            actions,
            focus: Vec::new(),
        });
    }

    fn repin_revealed_library_cards(&mut self) {
        let Some(suspended) = &self.state.suspended_decision else {
            return;
        };
        let player = suspended.decision.player();
        let revealed = suspended.decision.revealed_cards().to_vec();
        if revealed.is_empty() {
            return;
        }
        for card in &revealed {
            self.state.zones.move_card(*card, player, ZoneType::Library);
        }
        let revealed_set = revealed.iter().copied().collect::<BTreeSet<_>>();
        let library = self.state.zones.zone_cards_mut(ZoneType::Library, player);
        library.retain(|card| !revealed_set.contains(card));
        // revealed[0] is the top of the library = last element.
        for card in revealed.iter().rev() {
            library.push(*card);
        }
    }

    /// Play both sides uniformly-random-legal to terminal.
    ///
    /// Returns `Ok(Some(winner_index))` on a decided game, `Ok(None)` if the
    /// game is a draw or `max_steps` was reached without termination.
    /// `hit_cap` (when provided) is set to true only in the cap case.
    pub fn random_playout(
        &mut self,
        max_steps: usize,
        hit_cap: Option<&mut bool>,
    ) -> Result<Option<usize>, AgentError> {
        let mut steps = 0usize;
        while !self.is_game_over() {
            if steps >= max_steps {
                if let Some(flag) = hit_cap {
                    *flag = true;
                }
                return Ok(None);
            }
            let action_count = self
                .current_action_space
                .as_ref()
                .map_or(0, |space| space.actions.len());
            if action_count == 0 {
                return Err(AgentError("random_playout: empty action space".to_string()));
            }
            let index = self.state.rng.gen_range(0..action_count);
            self.step(index)?;
            steps += 1;
        }
        Ok(self.winner_index())
    }
}

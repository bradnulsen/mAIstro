INSERT OR REPLACE INTO goal_properties (goal_id, key, value) VALUES ('strategy', 'description',
'STRATEGY.md is the medium — an ordered list of priorities (each with problem statement, rationale, and scope), deferred items (with reasons), and non-goals (load-bearing decisions against scope creep).

### Principles

**Intent is sacred.** The operator sets direction. Precision increases through each pass — a rough bullet becomes a scoped priority, a reordering gets rationale — but direction never changes. Intent is never fabricated. Hard calls are never softened.

**Ruthless prioritization.** Everything competes for position. If two things are both important, one is still more important. Strategy that tries to please everyone provides no direction. Every priority displaces something — tradeoffs are explicit.

**Coherence is continuous.** The whole document stays internally consistent after every edit. A priority that conflicts with a non-goal gets surfaced. Overlapping scope gets merged. Contradictions resolve.

**Precision compounds downstream.** Loose language here becomes wrong code at the bottom of the chain. If something is fuzzy in strategy, it will be fuzzier in design. Every sentence is tight enough for the next reader to act without guessing.

**Upstream gets tightened too.** When operator intent contains ambiguity that was not deliberate, it gets resolved. When context makes the inference clear, the inference is made.

### What Good Strategy Looks Like

Diagnosis before prescription. Guiding policy over aspiration. Coherent action over laundry list. Leverage — actions that produce disproportionate results. Concentration — the vital few areas where superior performance matters. Opportunity cost — every yes is a no to something else. Second-order effects — good priorities set up the next move.

A good STRATEGY.md exhibits all of these. A priority without a clear diagnosis is premature. A list without ordering is not strategy. Scope without tradeoffs is wishful thinking.

### Standards

STRATEGY.md is a first-principle, as-is representation of current direction — not a task list, work log, or aspirational document.

When genuine ambiguity would send Design down fundamentally different paths, a review block surfaces. This is the exception — when context makes the inference reasonable, the inference is made. Review blocks are removed when subsequent edits resolve the ambiguity.

Commit style: `[Strategy] <what changed>`');

INSERT OR REPLACE INTO goal_properties (goal_id, key, value) VALUES ('design', 'description',
'DESIGN.md is the medium — requirements (what the product must do — behavioral promises precise enough to verify) and constraints (what the product must never violate — invariants and boundaries).

### Principles

**Essentials over accidents.** What is essential to the product is distinguished from what is accidental (current implementation, UI layout, tech stack). Only essentials belong here. Implementation details belong in Architecture.

**Fewer concepts, precisely defined.** Every concept earns its place. If two can be unified, they are unified. If a concept needs a name, it gets the exact right name. Concepts proliferate naturally — good design fights that entropy.

**Behavioral promises, not implementation.** Requirements describe what the product does, never how. "Tasks are never mutated after creation" is a design requirement. "Use an INSERT-only pattern" is implementation leaking upward.

**Aggressive translation.** Rough strategic intent is still intent. Most priorities, even when vague, imply a design direction. That direction is taken. Questions arise only when two completely different specs could result and there is no way to tell which one.

**Upstream gets tightened.** When strategic language has clear design implications that are not stated, they get made explicit in STRATEGY.md. Design is the first consumer of strategy — fuzziness here is worse for everyone downstream.

### What Good Design Looks Like

The product specified here has: clear affordances — every object makes its possible actions obvious. Natural mappings — the relationship between controls and effects is intuitive. Immediate feedback — actions produce clear, timely responses. Effective constraints — the design guides users toward correct action by restricting incorrect action. Visibility and discoverability — functions, state, and available actions are apparent. A coherent conceptual model — the user builds an accurate mental model of how the system works. Strong signifiers — the right cues tell users where and how to act.

A good DESIGN.md produces a product that exhibits these qualities. Requirements that leave affordances ambiguous are incomplete. Constraints that do not guide action are decoration.

### Standards

DESIGN.md is a first-principle, as-is representation of the product''s behavioral contract — not implementation notes or aspirational features.

When a strategic priority could be designed in fundamentally different ways and the choice matters, a review block surfaces in STRATEGY.md. This is rare. Review blocks are removed when subsequent edits resolve the ambiguity.

Commit style: `[Design] <what changed>`');

INSERT OR REPLACE INTO goal_properties (goal_id, key, value) VALUES ('architecture', 'description',
'`architecture/` is the medium. Each document describes what a system does, how it relates to other systems, and why it has this shape. Abstract enough to survive refactoring, concrete enough to build against. Files are created, moved, split, and merged as systems demand.

### Principles

**Forest, not trees.** All systems exist in relation to each other. A change to one system that ripples into three others is visible in the docs.

**Material, not ideal.** Unlike Design which thinks in essentials, architecture grapples with reality — tech stack constraints, performance characteristics, concurrency models, storage tradeoffs. Abstract intent meets material reality here, and tensions surface here more than anywhere else.

**Systems are not features.** Organization reflects technical structure, not product structure. A single requirement might span three systems. Two features might share one system.

**Skeptical by default.** Proposals and assumptions may be stale or incomplete. Blast radius is traced through every affected system. Actual code is read to verify — descriptions alone are not trusted.

**Upstream gets tightened.** When design language has clear structural implications that are not stated, they get made explicit in DESIGN.md. If a requirement implies a data invariant or concurrency constraint, it should say so.

### What Good Architecture Looks Like

The systems described here have: clear boundaries — where one system ends and another begins is never ambiguous. Legible flows — how data and control move is traceable. Self-regulating feedback loops — the system detects and corrects its own drift. High leverage points — small structural changes produce large systemic effects. Explicit forces — the tensions and tradeoffs that shaped each decision are documented. Clean seams — systems can be separated, composed, or replaced at well-defined interfaces. Maintained invariants — what must remain true across all states is stated and enforced. Conceptual integrity — the whole system appears designed by a single mind. Information hiding — modules hide design decisions behind stable interfaces.

A good `architecture/` exhibits all of these. Systems without clear boundaries grow into each other. Invariants that are not stated get violated. Decisions without documented forces get revisited endlessly.

### Standards

Architecture docs are first-principle, as-is descriptions of technical systems — not aspirational designs or implementation guides.

Proposals in `architecture/proposals/` are evaluated skeptically. They are rejected or revised when side-effects outweigh value.

When a requirement has hidden structural cost or two requirements create genuine tension, a review block surfaces in DESIGN.md. If Design''s priorities make the resolution clear, the resolution is made. Questions arise only when the tradeoff is genuinely Design''s call.

Commit style: `[Architecture] <what changed>`');

INSERT OR REPLACE INTO goal_properties (goal_id, key, value) VALUES ('pragmatic-engineering', 'description',
'Working code is the medium. Architecture specs arrive as system descriptions with boundaries, flows, and invariants. The code is the obvious, correct realization — cohesive and unmistakably corresponding to the spec.

### Principles

**Fidelity to spec.** Architecture docs are source of truth. What the architecture describes, the code implements. When the architecture changes a boundary, the code moves with it. When the architecture is silent, the code stays still. When it contradicts current code, the architecture wins.

**Implementation, not redesign.** New tables, new endpoints, changed component boundaries, new communication patterns — these are architecture, not implementation. They belong in a proposal in `architecture/proposals/`, not in code.

**Scoped changes.** Each commit addresses one concern. Related changes go together as logical units. What does not need touching does not get touched.

**Polish is delivery.** Self-documenting code. Comments for nuance, not narration. Simplicity through refactoring once the shape is clear. Not done until it works correctly and feels right.

**Upstream gets tightened.** When architecture specs are vague but the implementation is obvious, the spec gets made explicit. If a spec implies a specific function signature or query shape, it gets written in. The next task should not have to guess differently.

### What Good Code Looks Like

DRY — every piece of knowledge has a single, unambiguous representation. Orthogonal — components are independent; changing one does not ripple into others. Reversible — decisions stay soft where possible; hard commitments are deliberate. Built on tracer bullets — thin end-to-end slices that prove the path works before filling in. Good enough — gold-plating is a defect; knowing when to stop matters. Governed by contract — preconditions, postconditions, and invariants are clear at system boundaries. Free of broken windows — a single piece of neglect invites further neglect; the code is left better than it was found.

Code that duplicates knowledge will diverge. Components that are not orthogonal will fight each other during changes. Decisions that cannot be reversed will become regrets.

### Standards

Code is a first-principle, as-is realization of the architecture — not a prototype or proof of concept.

When a spec could be implemented in fundamentally different ways and the choice has real consequences, a review block surfaces in the architecture doc. This is uncommon — most specs have an obvious implementation, and that implementation is taken. Questions arise only when the choice constrains future architecture.

Commit style: `[Pragmatic Engineering] <what was implemented or fixed>`');

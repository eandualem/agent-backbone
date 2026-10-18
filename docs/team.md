# Start and develop your team

Agent Backbone brings agents across different AI CLIs and projects into one
personal team. It provides communication, coordination and continuity, with working
practices you can shape. Specialists can investigate, work together and preserve
what they learn, so later projects can draw on that experience.

Start with one repository and a useful collaboration. You do not need a management
hierarchy, a coordination repository or a fixed workflow. The models do the
reasoning; your goals, feedback and the team's retained work shape its expertise.
Backbone makes the team reachable across sessions and CLIs. It does not train
models or automatically decide which lessons are true.

## Choose your starting practices

The package includes three editable starter policies:

- **`starter-collaboration`** keeps responsibility clear, helps agents find and
  consult peers, and distinguishes a delivered request from a finished result.
- **`starter-continuity`** preserves current context, reasoning and commitments
  across fresh sessions without imposing a memory layout on repositories.
- **`starter-learning`** turns useful discoveries and corrections into scoped
  lessons, then tests and revises them through later work.

Read them and select the practices you want agents to receive:

```bash
backbone templates show starter-collaboration
backbone templates show starter-continuity
backbone templates show starter-learning
backbone templates use
backbone templates use starter-collaboration starter-continuity starter-learning
```

The last command **replaces the global assignment**. On an existing installation,
keep any policies you still want in that command, or assign the starters to a tag
instead. Installation and upgrades do not select them for you. Once selected, they
are included in fresh agent instructions through the existing brief mechanism.
See “Brief reaches a fresh session before other work” in
[runtime coverage](runtime-capabilities.md) for adapter limits.

```bash
backbone templates preview app       # after registering the agent
backbone templates edit starter-learning
backbone templates use starter-collaboration starter-continuity  # remove one
backbone templates use --clear       # remove all global policies
```

You can edit, replace or remove these practices. Remove an unwanted policy from
each applicable global/tag assignment. Deleting a local starter copy restores its
bundled text; deleting a custom-only policy blocks launch while it remains assigned.
A same-name local edit replaces its bundled text and stays unchanged on upgrades.
Without a local override, package updates supply the next version. Additional
policies are composed as text, so resolve contradictions explicitly rather than
assuming the last paragraph wins. Preview before a fresh start at a safe stopping
point; file edits do not change what an already-running agent has read.
[Template controls](templates.md) explain paths, tags and resume behavior.

These are working agreements, not permissions. Changing them does not grant
access, approve an operation or weaken runtime confinement. Read the
[security model](security.md) before sharing access to the service.

## Try a README collaboration

After [setup](getting-started.md), keep `app` responsible for its repository and
ask a second agent for README expertise. A specialist can use an ordinary directory
outside that repository; it does not need its own GitHub repository. Choose an
installed CLI, which can differ from the project agent's:

```bash
mkdir -p ~/agents/readme
backbone agent start readme --dir ~/agents/readme --runtime codex
backbone agent set readme description="README advice grounded in product facts and reader needs"
backbone agent list
```

In `app`'s conversation, give it this task:

```text
Help improve this repository's README. Establish its audience, useful outcome
and first-use steps from the project. Consult readme with those facts and the
constraints. Ask for advice and reasons, then check the proposed claims here
and bring me a draft. You remain responsible for the integrated result.
```

`app` can use `backbone tell readme "..."`; the specialist replies through the same
channel. A useful request contains the relevant text or public source links, not
an assumption that the specialist can read every project's private files. Read
the send receipt, then check the actual reply and draft. A queued message needs
no duplicate send. Keep parallel helpers scoped so they do not independently
direct or edit the same work.

Give concrete feedback on the draft. Ask the specialist to preserve a useful
lesson in the memory location you choose, including why it mattered and where it
may not apply. For example, a developer tool may benefit from a runnable command
near the opening; a product for nontechnical readers may need a different example.
Record the actual conclusion from your work rather than adopting this illustration
as a universal rule. Private notes should stay out of public commits.

## Bring the experience to another project

On a second repository, ask its agent to consult `readme` about a new audience and
goal. The specialist should use the earlier reasoning, check what transfers and
explain exceptions. The project agent owns its current facts and validation.
When feedback changes a lesson, update or supersede the old record and tell peers
whose advice depended on it. Promote reusable guidance to a [shared skill](skills.md)
only when useful; do not copy private facts or past approvals into it.

Before ending a specialist's session, preserve its decisions, sources, corrections
and next steps. A fresh session or another model needs to read that material; a
continuing name alone does not carry understanding. Shared skills and policies
provide reusable context, while project memory keeps local details and exceptions.

Check the result: did the second project receive relevant advice without you
repeating the whole history? Did the specialist adapt the lesson, avoid repeating
a corrected mistake, and explain its reasoning? Can you find and change the policy
behind the working model? If not, improve the retained context or practice before
adding more agents. More messages or installed policies are not evidence of better
work. Consult existing specialists first; use [swarms](swarms.md) for temporary
parallel work when their issue-based workflow fits.

"""The prompts. Daily summaries, roll-ups and XPU scoring keep the wording of the summaries already in the folder
(so old and new files read alike); the report adds structured sections and the focus. Summaries and reports mark what
is new to the project with "🆕" (and a "🆕 What's new" section), which the app shows highlighted."""

NEW = "🆕"

NEW_RULE = """- Highlight everything new to the project: a new feature, API, option, flag, environment variable, component,
  backend, device, file format, a new term or name, a new concept or design, a new RFC or proposal, or any other first
  appearance. Start the bullet (or sentence) about it with "🆕 " and bold the new name, e.g.
  "🆕 **Paged KV cache for FlexAttention**: …". Only things introduced or appearing for the first time count as new —
  not fixes or changes to existing things.

- When there is anything new, add a section "## 🆕 What's new" right after the introduction, one bullet per new thing:
  "- 🆕 **the new name** (new feature | new API | new term | new concept | …): what it is in one sentence, with its
  link". Leave the section out when nothing is new.
"""

DAILY = """
You are provided with a list of GitHub issues and pull requests (PRs), each detailed with specific information in the following format:

---
Title: [Issue or PR Title]
URL: [Issue or PR URL]
Description: [Detailed description]
Submitter: [Username of the person who submitted]
Tags: [Relevant tags]
Assignees: [Assigned users]
Reviewers: [Reviewers, if any]
Created At: [Creation date]
State: [Current state, e.g., open, closed]
Comments: [Number of comments]
Review Comments: [Number of review comments]
Commented by [Username] (created at [Date]): [Comment content]
...
---

Please generate a summary of the following list of GitHub issues and pull requests. The summary should:

- Be concise, be concise, be concise.

- Describe each issue or PR within two sentences.

- Mention the "URL" when referring to any issue or PR for easy reference, as a Markdown link, e.g. [PR #123](https://github.com/...).

- Logically group related issues and PRs under short Markdown headings (##) to enhance readability.

- Start with a top-level heading (#) naming the main themes of the day and one short paragraph introducing them.

""" + NEW_RULE + """
Below is the detailed information for generating the summary:

"""

COMBINE = """
Please combine the summaries of the individual GitHub issues and pull requests into a single summary.
Requirements:
 - You may rearrange the content according to their relevance and re-group them accordingly.
 - Please retain all the information of issues and PRs. DO NOT miss any. DO NOT miss any. DO NOT miss any.
 - Keep the Markdown links to issues and PRs.
 - Keep every "🆕" marker, and merge the "## 🆕 What's new" sections into one (without duplicates), placed right after
   the introduction.

Below are the concatenated summaries:

"""


def rollup(n: int, label: str, repo: str, since: str, until: str, days: int) -> str:
    return f"""
You are provided with {n} {label} summaries of GitHub activity for the {repo} repository,
covering the period from {since} to {until} ({days} days).

Please combine these {label} summaries into a single cohesive summary of the whole period. The summary should:

- Be concise but comprehensive. Cover the most important themes and changes across the entire period.

- Logically group related issues and PRs across different days under short Markdown headings.

- Mention the "URL" when referring to any issue or PR, as a Markdown link.

- Retain all significant issues and PRs. DO NOT miss any important items.

- Identify overarching trends and themes that emerged across the {days}-day period.

{NEW_RULE}
- Merge the "## 🆕 What's new" sections of the {label} summaries into the one of the period (without duplicates).

Below are the {label} summaries:

"""


COMMITS = """
You are given the commits that landed on the default branch of {repo} between {since} and {until} (hash, date, author,
subject; merges excluded) and the number of changed files per area. Summarize what changed, grouped by area, in at most
40 short bullet lines: new features and APIs, behaviour changes, performance, notable fixes, deprecations and removals,
build / CI. Start the bullet of anything new to the project (a new feature, API, option, flag, environment variable,
component, backend, term or concept) with "🆕 " and bold the new name. Cite PR numbers as Markdown links (https://github.com/{repo}/pull/<n>) when the subject has one, else the
commit hash. Output only the Markdown.

"""

SCORE = """
You are assisting the PyTorch XPU (Intel GPU) maintainer.

CRITICAL OUTPUT REQUIREMENTS (must follow):
  - Be extremely clear and concise. No fluff. No long paragraphs.
  - Output MUST use the exact template below.
  - For each item, use <= 6 bullet lines total. If you need more, it is NOT concise enough.

Objective:
  - Monitor PyTorch activities and identify changes/issues that are critical to Intel GPU (XPU).
  - Specifically, track CUDA/ROCm changes and shared components(like Inductor/Triton, torchgen, ATen schemas, etc) that may impact XPU correctness or performance.
  - Ignore MPS and MTIA related changes as they are not relevant to Intel GPU, even if the changes touched focus areas as follow.

Focus Areas (only include items related to these):
  - CUDA backend
  - ROCm backend
  - Inductor and Triton backend (shared GPU backend)
  - Performance-sensitive heuristic rules (tiling, num_warps, reduction strategy, autotune defaults)
  - TorchInductor scheduler / codegen / autotuning
  - Profiler/Kineto
  - torchgen / build registration / DispatchKey codegen
  - ATen operator schema & dispatch (native_functions.yaml)
  - Distributed / comm backends
  - Precision features (FP8/TF32/etc.)
  - Device semantics changes

Pattern Triggers (treat as high-signal):
  A) Hardcoded CUDA checks / CUDA-only branches (missing XPU/generalization)
  B) Hardcoded "cuda" in tests or runtime settings (should use GPU_TYPE / parametrization)
  C) ATen schema changes (alias annotations, mutation semantics, signatures) without XPU validation
  D) torchgen / registration infra changes that special-case CUDA or exclude external backends
  E) Inductor/Triton heuristic changes optimized for CUDA that may regress XPU performance

Examples below illustrate what counts as a Trigger and how to generalize for XPU.
  EXAMPLE 1 (Trigger A: CUDA-only branch in shared code)
  Problem: Code checks only CUDA and runs backend-specific ops.
  Fix: Include XPU or generalize via dispatch/device tags.

  Original (CUDA-only):
  ```C++
  if (act == torch::autograd::profiler::ActivityType::CUDA) {
    torch::profiler::impl::kineto::toggleCollectionDynamic(enable);
  }
  ```

  To make it compatible with Intel GPUs, the code should check for the Intel GPU device tag, which is "XPU". Below is the refined version:
  ```C++
  if (act == torch::autograd::profiler::ActivityType::CUDA ||
      act == torch::autograd::profiler::ActivityType::XPU) {
    torch::profiler::impl::kineto::toggleCollectionDynamic(enable);
  }
  ```

  EXAMPLE 2 (Trigger B: hardcoded "cuda" in Inductor tests)
  Problem: Test assumes CUDA-only.
  Fix: Use GPU_TYPE abstraction (Inductor unifies XPU and CUDA under GPU_TYPE).

  Original:
  ```python
  torch.set_default_device("cuda")
  ```

  Refined:
  ```python
  torch.set_default_device(GPU_TYPE)
  ```

  EXAMPLE 3 (Trigger C: ATen schema / alias semantics change)
  Problem: native_functions.yaml modifies alias annotations / mutation semantics.
  Risk: can break backend compatibility or require kernel adjustments.

  Example diff:
  ```git.diff
  -- func: rrelu_with_noise.out(Tensor self, Tensor noise, Scalar lower=0.125, Scalar upper=0.3333333333333333, bool training=False, Generator? generator=None, *, Tensor(a!) out) -> Tensor(a!)
  +- func: rrelu_with_noise.out(Tensor self, Tensor(b!) noise, Scalar lower=0.125, Scalar upper=0.3333333333333333, bool training=False, Generator? generator=None, *, Tensor(a!) out) -> Tensor(a!)
     python_module: nn
     tags: nondeterministic_seeded
     dispatch:
       CPU: rrelu_with_noise_out_cpu
       CUDA: rrelu_with_noise_out_cuda
  ```

  EXAMPLE 4 (Trigger D: torchgen / registration infra becomes CUDA-special-cased)
  Problem: torchgen generates CUDA-specific files or sharding logic without considering external backends like XPU.

  Example diff:
  ```git.diff
  - RegisterCUDA.cpp
  + RegisterCUDA_0.cpp
  ...
  fm.write_sharded_with_template(... dispatch_key == DispatchKey.CPU else 1 ...)
  ```

  EXAMPLE 5 (Trigger E: Inductor/Triton heuristic change may regress XPU)
  Problem: CUDA-optimized tiling/warps/reduction strategy may hurt XPU (shared Triton backend).
  Action: require XPU benchmarking and possibly a device-specific heuristic override.

I will share the detailes of a PyTorch pull request or a PyTorch issue with you. The detailed information starts from <<<BEGIN>>> and is ended by <<<END>>>.
The format of the detailed information is as follows.
  <<<BEGIN>>>
  Title: [Issue or PR Title]
  Description: [Detailed description]
  Tags: [Relevant tags]
  Diff: [Detailed changes in the PR in git diff format]
  <<<END>>>

You need to help me evaluate their relevance on Intel GPUs. And the evaluation process must be seriously conducted because I cannot review too many issues and pull requests.
The relevance or impact needs be represented as a score number on a scale of 0 to 100.
- 0-24: Low relevance or impact for Intel GPU.
- 25-50: Moderate relevance or impact for Intel GPU.
- 51-74: Some relevance or impact for Intel GPU.
- 75-89: High relevance or impact for Intel GPU.
- 90-100: Critical to Intel GPU.

Besides that score, you need to elaborate on the score number to express why you think the pull request or issue is low/moderate/high relevant to Intel GPU.

And your answer should be in JSON format. The structure is as follows.
- "score": represents the relevance or impact
- "justification": represents your elaboration on the score number

Below is an example except FYI ("..." is used for brevity).
{
    "score": 99,
    "justification": "This PR is critical to Intel GPU. It intends to ... and it introduces ..., which is a critical feature for optimizing performance on Intel GPU."
}

Please ensure your anwser to strictly follow this JSON format. Because I will develop some codes to parse the format and
use the score and justification to evaluate the relevance or impact of the shared pull request or issue to Intel GPU.
"""

BODY = """
Provide a single, clear, and concise sentence summarizing the GitHub issue or PR in plain text.
I will share the description of a pull request or an issue with you.
The detailed information starts from <<<BEGIN>>> and is ended by <<<END>>>.
"""

XPU_FOCUS = (
    "Intel GPU (XPU): what matters to the PyTorch XPU maintainer — CUDA and ROCm backend changes, Inductor / Triton "
    "(shared GPU backend, heuristics tuned for CUDA: tiling, num_warps, reduction strategy, autotune defaults), "
    "TorchInductor scheduler / codegen / autotuning, profiler / Kineto, torchgen / build registration / DispatchKey "
    "codegen, ATen operator schemas and dispatch (native_functions.yaml), distributed / communication backends, precision "
    "features (FP8, TF32), device semantics. High-signal patterns: A) CUDA-only branches in shared code, B) hard-coded "
    "\"cuda\" in tests or settings (should use GPU_TYPE), C) ATen schema changes without XPU validation, D) torchgen / "
    "registration special-casing CUDA, E) Inductor/Triton heuristics optimized for CUDA. Ignore MPS and MTIA. For each "
    "highlight add one line: Action for XPU (Ignore / Monitor / Patch XPU / Generalize upstream / Benchmark on XPU).")


def report(repo: str, since: str, until: str, days: int, focus: str, has: dict[str, bool], known_note: str = "") -> str:
    sections = ["## Highlights :: the 5 to 10 most important changes of the period" + (", ranked by the focus" if focus else ""),
                "## 🆕 What's new :: everything new to the project in the period (new features, APIs, options, components, "
                "terms, concepts, RFCs or proposals, any other first appearance), one bullet each: "
                "🆕 **the new name** (new feature | new API | new term | new concept | …): what it is, with its link"]
    if has.get("commits"):
        sections.append("## Changes by area :: what landed (from the commits): new features and APIs, behaviour changes, "
                        "performance, notable fixes, deprecations and removals, build / CI")
    if has.get("issues"):
        sections.append("## Issues and pull requests :: new and significant, closed / resolved, active discussions")
    if has.get("rfcs"):
        sections.append("## RFCs :: new, updated, decided")
    if has.get("highlights"):
        sections.append("## XPU watch :: the scored items that matter most for Intel GPU, with their score")
    sections += ["## Trends :: 3 to 5 bullets", "## Numbers :: from the facts given"]
    return (
        f"Write a report of what happened in the GitHub repository {repo} from {since} to {until} ({days} days), from the "
        "material below (it was prepared from GitHub and the git history; do not invent anything). Use exactly these "
        "Markdown sections, in this order (the heading is the text before \"::\" exactly; after it is what goes in the "
        "section), and leave out a section only when its material is empty:\n"
        + "\n".join(sections) + "\n\nRules: be concise; bullets of one or two sentences; every issue, PR, RFC or commit you "
        "mention is a Markdown link (e.g. [PR #123](https://github.com/" + repo + "/pull/123), "
        "[issue #45](https://github.com/" + repo + "/issues/45)); group related items; do not repeat an item across "
        "sections except in Highlights and What's new; in the other sections start the bullet of anything new with \"🆕 \" "
        "and bold the new name. Only things that first appear in the period count as new — not changes to existing "
        "things, and not what is listed below as known. Start with a one-line title (#) naming the "
        "period's main themes."
        + (f"\n\nThe reader's focus — rank and select by it, and say what each highlight means for it:\n{focus}" if focus else "")
        + known_note + "\n\nMaterial:\n\n")


def daily(names: list[str], baseline: str | None = None, days: int = 0) -> str:
    """The daily instruction, with what is known: what the summaries of the `baseline` (e.g. everything before the day)
    are about; without it, the names earlier days flagged as new."""
    note = known(names, baseline, days) if baseline else known(names)
    return DAILY.replace("Below is the detailed information", note + "\nBelow is the detailed information", 1) if note else DAILY


def known(names: list[str], baseline: str | None = None, days: int | None = None) -> str:
    """The instruction's note of what is already known (so it is not marked new): the names flagged new before, or —
    with `baseline` — every feature, API, term and concept of the baseline period."""
    if baseline is None:
        if not names:
            return ""
        return ("\nAlready flagged as new before this material (known now — do not mark these as new again, unless the "
                "material introduces something new about them): " + "; ".join(names) + "\n")
    if not days:
        return (f"\nWhat is new is judged against the baseline, {baseline}, but there are no summaries of it: mark as new "
                "what the material itself shows appearing for the first time, and say in What's new that the baseline had no data.\n")
    return (f"\nWhat is new is judged against the baseline: {baseline} ({days} day(s) with a summary). These features, APIs, "
            "options, components, terms and concepts already appeared in the baseline, so they are known, not new (unless "
            "the material introduces something new about them). Mark as new (🆕, and list in What's new) what the material has "
            "that is not among them; the 🆕 marks already in the material are hints only — decide by the baseline.\n"
            "Known in the baseline: " + ("; ".join(names) if names else "(nothing)") + "\n")


TERMS = """
List the things the summary below is about that a reader could later ask "is this new?": features, APIs, functions,
options, flags, environment variables, components, backends, devices, file formats, named terms and concepts, RFCs and
proposals. Names only (as written, at most 8 words each), the most specific ones, no issue or PR numbers, no people.
Answer with ONLY a JSON object: {"terms": ["…", "…"]}.

Summary:

"""


def describe(repo: str) -> str:
    """What a daily summary is about, with what each thing is (the term registry is made of it)."""
    return DESCRIBE.replace("{repo}", repo)


DESCRIBE = """List the things the daily summary of {repo} below is about that a reader could later ask "is this new?":
features, APIs, functions, options, flags, environment variables, components, backends, devices, file formats, named
terms and concepts, RFCs and proposals. The most specific ones, no people. For each: its name as written (at most 8
words), other spellings or short forms used for it, its kind, its area (e.g. FSDP, Inductor, XPU, CI), one sentence
on what it is, and the issue/PR numbers the summary links for it (at most 3).
Answer with ONLY a JSON object: {"terms": [{"name": "…", "aliases": ["…"], "kind": "feature|API|option|component|backend|concept|other",
"area": "…", "what": "…", "refs": [123]}]}

Daily summary:
"""


def check(repo: str, what: str, baseline: str) -> str:
    """The instruction of the new-things check (candidates and their closest registry entries follow)."""
    return CHECK.replace("{repo}", repo).replace("{what}", what).replace("{baseline}", baseline)


CHECK = """You check what {what} of {repo} marks as NEW against a registry of what the earlier daily summaries ({baseline})
were already about. For each candidate you get the sentence it is marked in and the closest registry entries (name,
other spellings, kind, area, what it is, first and last day seen, on how many days).
Decide for each candidate:
- "repeat": it is the same thing as an entry (maybe worded differently) and the sentence adds nothing essentially new
- "extension": an entry is the same base thing, but the candidate is a genuinely new capability, variant or part of it
- "duplicate": it is the same thing as an EARLIER candidate of this text (give that candidate's id as "same_as")
- "new": none of the entries (and no earlier candidate) is it
Give a confidence 0-100, the matching entry's number (for repeat / extension, else null) and a short reason.
Answer with ONLY a JSON object: {"results": [{"id": 1, "verdict": "repeat|extension|duplicate|new", "match": 3, "same_as": null,
"confidence": 90, "why": "…"}]}

"""

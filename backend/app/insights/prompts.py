"""The prompts. Daily summaries, roll-ups and XPU scoring keep the wording of the summaries already in the folder
(so old and new files read alike); the report adds structured sections and the focus."""

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

Below is the detailed information for generating the summary:

"""

COMBINE = """
Please combine the summaries of the individual GitHub issues and pull requests into a single summary.
Requirements:
 - You may rearrange the content according to their relevance and re-group them accordingly.
 - Please retain all the information of issues and PRs. DO NOT miss any. DO NOT miss any. DO NOT miss any.
 - Keep the Markdown links to issues and PRs.

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

Below are the {label} summaries:

"""


COMMITS = """
You are given the commits that landed on the default branch of {repo} between {since} and {until} (hash, date, author,
subject; merges excluded) and the number of changed files per area. Summarize what changed, grouped by area, in at most
40 short bullet lines: new features and APIs, behaviour changes, performance, notable fixes, deprecations and removals,
build / CI. Cite PR numbers as Markdown links (https://github.com/{repo}/pull/<n>) when the subject has one, else the
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


def report(repo: str, since: str, until: str, days: int, focus: str, has: dict[str, bool]) -> str:
    sections = ["## Highlights :: the 5 to 10 most important changes of the period" + (", ranked by the focus" if focus else "")]
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
        "sections except in Highlights. Start with a one-line title (#) naming the period's main themes."
        + (f"\n\nThe reader's focus — rank and select by it, and say what each highlight means for it:\n{focus}" if focus else "")
        + "\n\nMaterial:\n\n")

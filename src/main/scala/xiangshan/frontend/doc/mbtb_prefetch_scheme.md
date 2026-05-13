# mBTB + TAGE Pre-fetch Buffer Scheme for Frontend IPC Enhancement

## 1. Background and Problem

### Current Prediction Structure

The XiangShan frontend BPU combines fast and slow/high-accuracy paths across pipeline stages.

| Stage | Key Components | Role |
| --- | --- | --- |
| S1 | uBTB + ABTB + uTAGE + uRAS | Fast next fetch PC generation |
| S2 | mBTB + TAGE + SC | Larger BTB and history-based direction correction |
| S3 | latched mBTB/TAGE/SC + ITTAGE + RAS | Indirect/return target correction and final prediction assembly |

The S1 prediction is forwarded to the FTQ first. If the S3 final prediction differs from the S1 prediction, `s3_override` is triggered.

### The Override Problem

- When S1 and S3 results differ, `s3_override` fires.
- On override, in-flight S1/S2 predictions inside the BPU are flushed, and FTQ/IFU/prefetch pointers are rolled back as needed.
- Frequent overrides degrade frontend bandwidth and increase pipeline bubbles.

The goal of this document is **not** to move the entire S3 final prediction earlier, but to **pre-prepare mBTB + TAGE level predictions to improve S1 prediction quality and reduce s3_override frequency**.

---

## 2. Proposed Idea: mBTB + TAGE Pre-fetch Buffer Scheme

### Core Idea

Each uBTB entry stores an additional **3-block ahead PC** (relative to the current fetch block) and a TAGE folded-history delta.
When the uBTB entry is read at S1, this metadata is used to opportunistically issue an mBTB + TAGE prefetch for `PC_{X+3}`, and the result is stored in a 32-entry CAM buffer.

When the regular fetch reaches `PC_{X+3}` three fetch blocks later and the buffer hits, the precomputed mBTB + TAGE result replaces the original S1 prediction.

**Why 3-ahead over 4-ahead**: The number of intermediate branches between PC_X and PC_{X+3} is reduced by one, improving historyDelta accuracy and lowering the chance of speculative folded history errors.

Key constraints:

- Prefetch targets **mBTB + TAGE only**.
- SC, ITTAGE, and RAS are excluded from the prefetch path.
- S3 override is always permitted, as before.
- This scheme does not suppress the S3 final path for correctness.
- The expected benefit is that more accurate S1 predictions naturally reduce S3 override frequency.

### Timing Analysis

```
Cycle N:   PC_X → S0

Cycle N+1: PC_X → S1
           uBTB hit → aheadPc = PC_{X+3}, foldedHistoryDelta acquired
           speculative folded history generated

Cycle N+2: PC_{X+1} → S0 (regular)
           PC_{X+3} prefetch request → mBTB S0 (sideband)
           Issued at mBTB S0 timing: regular read has priority, dropped on conflict

Cycle N+3: PC_{X+2} → S1 (regular)
           PC_{X+3} prefetch → mBTB S1 (in-flight)

Cycle N+4: PC_{X+3} → S1 (regular fetch arrives)
           PC_{X+3} prefetch → mBTB S2 (result ready) → buffer write
           ─────────────────────────────────────────────────
           buffer write and buffer read (CAM lookup) occur in the same cycle
           → same-cycle write-read bypass is required
           ─────────────────────────────────────────────────
           CAM HIT  → replace S1 prediction with buffer result
           CAM MISS → use original uBTB/ABTB/uTAGE path
```

**Bypass design**: write key = `{PC_{X+3}, historySignature}`, read key = `{s1_startPc, expectedSignature}`.
If write and read keys match in the same cycle, write data is forwarded as the read result before the CAM array commit completes.
The TAGE prefetch must be aligned to the same timing (mBTB S0 sideband issue, S2 result ready).

**3-ahead timing characteristic**: Prefetch completion (cycle N+4) and fetch S1 arrival (cycle N+4) occur in the same cycle, making bypass mandatory with no alternative. The S0 lookup / S1 use structure is impossible for 3-ahead because S0 of PC_{X+3} is cycle N+3 while prefetch completes at N+4 — this structure is only feasible by switching to 4-ahead (completion N+4, S0 lookup N+4, S1 use N+5), which moves the bypass burden to the S0 level. As long as 3-ahead is chosen, S1 bypass is a design requirement. The compensating advantage over 4-ahead is that the historyDelta path is one block shorter, improving speculative history accuracy.

### Components

#### (1) Pre-fetch Buffer

- Structure: 32-entry fully-associative CAM
- Hit key: `{pcTag, historySignature}`
  - `pcTag`: the prefetch target `PC_{X+3}`
  - `historySignature` matching:
    - **write key**: `phr.io.prefetchHistorySignature` = `hash(s0_foldedPhr XOR foldedHistoryDelta)` computed at prefetch issue (cycle N+2), pipelined to N+4
    - **read key**: `hash(s1_foldedPhr)` — actual PHR state at S1 of PC_{X+3} (cycle N+4)
    - Algorithm core: if speculation is correct, both values match → hit. If actual history diverges due to redirect or misprediction, mismatch → miss → fallback to original uBTB path
- Data:
  - `taken`
  - `target`
  - `cfiPosition`
  - `attribute`
- Replacement: PLRU or round-robin
- Read timing: same cycle as S1 prediction selection
- Write timing: cycle when mBTB + TAGE prefetch pipeline completes
- Same-cycle write-read bypass supported
- No flush needed on redirect: historySignature mismatch causes automatic miss. S3 override guarantees correctness.

#### (2) uBTB Entry Extension

The existing uBTB entry stores branch position/attribute/target information for the current fetch block.
In this scheme, the uBTB also carries **successor/path metadata** not present in the mBTB.

Added fields:

```scala
val aheadValid: Bool
val aheadPc:    PrunedAddr // full/pruned fetch-block PC
val foldedHistoryDelta: Vec[...] // precomputed delta for TAGE folded history correction
val historySignature: UInt       // short signature for prefetch issue validity filtering
```

`aheadPc` is stored as a full/pruned PC, not a partial target encoding, for direct use in mBTB/TAGE prefetch requests and CAM tag comparison.

#### (3) Folded History Delta

TAGE builds table indices and tags from `hash(PC, folded history)`.
Predicting `PC_{X+3}` in advance therefore requires the folded history at the `PC_{X+3}` point.

Storing only taken/NT bits is insufficient, because the PHR update is influenced by a path hash that includes branch target and CFI PC.

`foldedHistoryDelta` in this scheme represents:

- A precomputed delta capturing the effect of branches along the committed path from `PC_X` to `PC_{X+3}` on the TAGE folded history
- At runtime, this delta is applied to the current `s0_foldedPhr` to produce a speculative folded history for `PC_{X+3}`
- Rather than reconstructing the full GHR, this corrects only the per-table folded histories used by TAGE

**Packed UInt Encoding**:

Let N be the number of TAGE tables and `fh_w_i` be the folded history width for table i:

```
FoldedHistoryDeltaWidth = sum(fh_w_i for i in 0..N-1)

foldedHistoryDelta = [ delta_{N-1} | ... | delta_1 | delta_0 ]   // LSB = table 0
```

Each `delta_i` is an XOR mask computed at commit time:

```
delta_i = foldedHistory_at_X[i] XOR foldedHistory_at_{X+3}[i]
```

Runtime apply:

```
prefetchFoldedPhr[i] = s0_foldedPhr[i] XOR delta_i
```

Since folded history updates are XOR-based, the delta itself is represented as an XOR mask.
Per-table slice ranges are determined statically from `AllFoldedHistoryInfo`.

`historySignature` is a short hash/signature derived from this speculative folded history. It is included in the buffer hit condition to reduce use of stale or wrong-path prefetch results.

#### (4) Commit-based 3-ahead Training

`aheadPc` and `foldedHistoryDelta` are trained based on the committed path.

Rationale:

- This metadata is not a branch entry natively held by the mBTB.
- It stores the "committed successor path 3 fetch-blocks ahead" from the current block in each uBTB entry.
- Training from wrong-path resolve results would plant incorrect 3-ahead paths in the uBTB.

Information required for training:

- `startPc = PC_X`
- `aheadPc = PC_{X+3}`
- `foldedHistoryDelta`
- `historySignature`

The FTQ commit side tracks the `commitPtr ~ commitPtr+3` window and updates the uBTB 3-ahead metadata when all 4 fetch blocks in the window have committed.

### Operational Flow Summary

```
[Prefetch Issue]
S1 uBTB hit for PC_X
  ├─ check aheadValid
  ├─ aheadPc = PC_{X+3}
  ├─ generate speculative folded history from foldedHistoryDelta
  └─ attempt mBTB + TAGE prefetch read
       ├─ no bank conflict → issue
       └─ bank conflict   → drop

[Buffer Fill]
mBTB + TAGE prefetch result ready
  ├─ if indirect/return → skip buffer write
  ├─ taken=true  → store branch prediction
  └─ taken=false → store as fallThrough-style prediction

[Buffer Use]
S1 fetch PC_{X+3}
  ├─ CAM lookup key = {PC_{X+3}, expectedHistorySignature}
  ├─ HIT  → replace S1 prediction with buffer result
  └─ MISS → use original uBTB/ABTB/uTAGE result

[S3 Check]
S3 final path is always preserved
  ├─ SC can flip direction
  ├─ ITTAGE/RAS can correct target
  └─ if S3 prediction != S1 prediction → s3_override fires as usual
```

### Prediction Usage Policy

#### Buffer Hit Priority

On a buffer hit, the prefetch result takes priority over the original S1 result.

```scala
when(prefetchBufferHit) {
  s1_prediction := prefetchBufferPrediction
}.otherwise {
  s1_prediction := originalS1Prediction
}
```

`s3_override` is never suppressed.

#### Indirect / Return Handling

Prefetch results are used only for conditional and direct branches.

- conditional: apply mBTB candidate + TAGE direction
- direct: use mBTB target as taken
- indirect: not used
- return: not used

Indirect and return branches depend heavily on ITTAGE/RAS and are delegated to the S3 final path.

#### Not-taken Handling

`taken=false` results are stored in the buffer and used.

However, not-taken results are stored as fallThrough-style predictions without preserving branch identity:

- `taken = false`
- `attribute = None`
- `cfiPosition = fallThrough.cfiPosition`
- `target = fallThrough.target`

This aligns well with the existing `Prediction ===` comparison and allows the scheme to advance cases where uBTB/ABTB predicts taken but mBTB+TAGE determines not-taken.

### Expected Benefits

- Partially eliminates mBTB/TAGE-level taken/not-taken mismatches at S1
- Allows earlier use of the more accurate cfiPosition found by mBTB at S1
- Reduces some direct branch target mismatches
- Expected reduction in S3 override frequency

### Remaining Overrides

S3 override can still occur in the following cases:

- SC flips the TAGE direction
- ITTAGE corrects an indirect target
- RAS corrects a return target
- Buffer result is stale or historySignature passed but actual S3 result differs
- Prefetch was dropped due to bank conflict, resulting in a buffer miss

### Risks

- The S1 critical path gains a 32-entry CAM compare and a prediction mux.
- uBTB entry width increases significantly.
- mBTB/TAGE banking expansion may impact area and timing.
- Coverage is sensitive to bank conflict rate, since dropped prefetches are not retried.
- foldedHistoryDelta generation and commit window tracking increase FTQ commit-side complexity.
- **Limited effectiveness due to indirect/return exclusion**: In workloads where ITTAGE/RAS corrections account for a large share of s3_overrides (e.g., C++ virtual dispatch, recursion-heavy code), the effective override reduction from this scheme may be negligible. The distribution of override causes by type should be measured via simulation before committing to implementation.

---

## 3. Related Work Comparison

### 3.1 LLBP — MICRO 2024

**"The Last-Level Branch Predictor"**  
David Schall, Andreas Sandberg, Boris Grot (University of Edinburgh)

- [ACM DL](https://dl.acm.org/doi/10.1109/MICRO61859.2024.00042)
- [GitHub](https://github.com/dhschall/LLBP)

#### LLBP Approach

- Uses TAGE as a fast predictor with a large LLBP storage as a backing store
- Stores prediction patterns in LLBP indexed by branch program context
- Prefetches LLBP results using a lookahead of 4 unconditional branches ahead
- Stores results in a small in-core PatternBuffer, accessed in parallel with TAGE
- On PatternBuffer hit, uses LLBP result; on miss, uses TAGE result only

#### Similarities — LLBP

| Item | LLBP | Proposed Scheme |
| --- | --- | --- |
| Hierarchy | TAGE fast + LLBP slow | S1 fast path + mBTB/TAGE prefetch |
| Prefetch approach | N branches ahead | 3 fetch blocks ahead |
| Buffer location | PatternBuffer | mBTB/TAGE prefetch buffer |
| Hit guard | context-based | PC tag + historySignature |
| Goal | hide slow predictor latency | pull mBTB/TAGE result forward to S1 |

#### Differences — LLBP

| Item | LLBP | Proposed Scheme |
| --- | --- | --- |
| Goal | Improve prediction accuracy | Reduce S3 override frequency |
| Prefetch granularity | branch/context pattern | fetch block PC |
| Backing store | dedicated LLBP | existing mBTB/TAGE |
| Access policy | dedicated backing store access | conflict-free opportunistic access |
| Final override | used in parallel with TAGE | S3 final path always preserved |

---

### 3.2 Two Level Bulk Preload — HPCA 2013

**"Two Level Bulk Preload Branch Prediction"**  
Bonanno, Collura et al. (IBM, zEnterprise EC12)

- [HPCA 2013 PDF](https://class.ece.iastate.edu/tyagi/cpre581/papers/HPCA13BulkPreloadBranch.pdf)
- [IEEE Xplore](https://ieeexplore.ieee.org/document/6522308/)

#### HPCA 2013 Approach

- Two-level BTB1 + BTB2 structure
- On BTB1 miss, bulk preloads from BTB2
- Limits BTB2 accesses to improve power efficiency
- Pre-populates a preload buffer with high-probability-hit entries

#### Similarities — HPCA 2013

| Item | HPCA 2013 | Proposed Scheme |
| --- | --- | --- |
| Hierarchy | BTB1 + BTB2 | S1 BTB + mBTB |
| Intermediate buffer | BTB preload buffer | mBTB/TAGE prefetch buffer |
| Goal | reduce redirect latency | reduce S3 override |
| Access policy | lower-level BTB preload | mBTB/TAGE opportunistic prefetch |

#### Differences — HPCA 2013

| Item | HPCA 2013 | Proposed Scheme |
| --- | --- | --- |
| Prefetch trigger | reactive on BTB1 miss | proactive via uBTB threeAhead |
| Prefetch target | BTB entry | mBTB + TAGE prediction result |
| History handling | BTB-centric | requires folded history delta |
| Conflict policy | depends on paper structure | regular read priority, drop on conflict |

---

### 3.3 Branch Pre-Prediction — KCI 2009

**"Branch Prediction Latency Hiding Scheme using Branch Pre-Prediction and Modified BTB"**

- [KCI Journal](https://journal.kci.go.kr/jksci/archive/articleView?artiId=ART001388533)

#### Similarities — KCI 2009

| Item | KCI 2009 | Proposed Scheme |
| --- | --- | --- |
| Core direction | prepare prediction before fetch | prepare mBTB/TAGE result before fetch |
| BTB modification | Modified BTB | uBTB threeAhead metadata |
| Goal | prediction latency hiding | use slow-path result at S1 |

#### Differences — KCI 2009

| Item | KCI 2009 | Proposed Scheme |
| --- | --- | --- |
| Target latency | single predictor latency | mBTB/TAGE slow-path latency |
| Decoupling | predictor/fetch decoupling | prefetch buffer based |
| Final correction | varies by structure | S3 final override always preserved |

---

## 4. Summary Comparison

| Paper | Mechanism Similarity | Goal Similarity | Reference Priority |
| --- | --- | --- | --- |
| LLBP (MICRO 2024) | High | Medium | 1st: reference lookahead + buffer + history guard |
| HPCA 2013 | Medium | High | 2nd: reference two-level BTB preload perspective |
| KCI 2009 | Medium | Medium | 3rd: reference latency hiding direction |

The proposed scheme applies LLBP's lookahead/buffer concept to XiangShan's mBTB/TAGE slow path, but without adding dedicated read ports — using banking expansion and conflict-free opportunistic issue instead.

---

## 5. Chisel Architecture Changes

This section covers only Chisel RTL-level changes. The gem5 model must be separately re-defined to functional equivalence after reviewing its code; specific implementation file or class names are not assumed here.

### 5.0 Scope Decision

Implementation scope:

- Included: mBTB + TAGE prefetch
- Excluded: SC, ITTAGE, RAS prefetch
- On buffer hit at S1, replace prediction with prefetch result
- S3 final path is always preserved
- `s3_override` is not suppressed

This implementation is a correctness-preserving optimization. Even if a buffer result is wrong, the existing S3 override will make the final correction.

---

### 5.1 `bpu/ubtb/Parameters.scala` — MicroBtbParameters

Added parameters:

```scala
case class MicroBtbParameters(
  ...
  EnableThreeAheadPrefetch: Boolean = false,
  HistorySignatureWidth: Int = 12,
  FoldedHistoryDeltaWidth: Int = ...
)
```

`FoldedHistoryDeltaWidth` is determined by the per-table folded history delta encoding for TAGE.

---

### 5.2 `bpu/ubtb/Bundles.scala` — MicroBtbEntry

Add 3-ahead metadata to `MicroBtbEntry`:

```scala
val aheadValid: Bool = Bool()
val aheadPc:    PrunedAddr = PrunedAddr(VAddrBits)

// Precomputed delta for correcting TAGE folded history to the PC_{X+3} reference point
val foldedHistoryDelta: UInt = UInt(FoldedHistoryDeltaWidth.W)

// Short signature for prefetch issue validity filtering
val historySignature: UInt = UInt(HistorySignatureWidth.W)
```

Notes:

- `aheadPc` is a full/pruned PC, not a partial target.
- The increase in uBTB entry width is significant; area evaluation is required.

---

### 5.3 `bpu/ubtb/MicroBtb.scala` — threeAhead Metadata Path

Predict side:

- Output `aheadValid`, `aheadPc`, `foldedHistoryDelta`, `historySignature` from the uBTB hit entry.
- Prefer a separate output from the existing `io.prediction`.

Train side:

- Existing `fastTrain` continues training slot1 predictions.
- 3-ahead metadata is updated via a separate commit-based train channel.

Example IO:

```scala
val aheadInfo: Valid[UbtbAheadInfo] = Output(Valid(new UbtbAheadInfo))
val aheadTrain: Valid[UbtbAheadTrain] = Input(Valid(new UbtbAheadTrain))
```

---

### 5.4 New Module — `bpu/mbtb/MbtbTagePrefetchBuffer.scala`

Add a 32-entry fully-associative CAM buffer.

Entry:

```scala
class MbtbTagePrefetchBufferEntry extends Bundle {
  val valid: Bool = Bool()

  val pcTag: UInt = UInt(VAddrBits.W)
  val historySignature: UInt = UInt(HistorySignatureWidth.W)

  val taken: Bool = Bool()
  val target: PrunedAddr = PrunedAddr(VAddrBits)
  val cfiPosition: UInt = UInt(CfiPositionWidth.W)
  val attribute: BranchAttribute = new BranchAttribute
}
```

Read:

```scala
val readPc: PrunedAddr
val readHistorySignature: UInt
val readHit: Bool
val readData: Prediction
```

Write:

```scala
val writeValid: Bool
val writePc: PrunedAddr
val writeHistorySignature: UInt
val writeData: Prediction
```

Required behavior:

- CAM compare key: `{pcTag, historySignature}`
- same-cycle write-read bypass supported
- No flush needed on redirect (historySignature staleness guard is sufficient; S3 override guarantees correctness)
- replacement: PLRU or round-robin

Timing risk:

- The S1 prediction path gains a CAM compare and a mux.
- Bypass is mandatory for 3-ahead; S0 lookup / S1 use is timing-infeasible (prefetch completes after S0).
- If timing closure fails, the only viable fallback is switching the scheme to 4-ahead (completion N+4, S0 lookup N+4, S1 use N+5, moving bypass burden to the S0 level).

---

### 5.5 `bpu/mbtb/MainBtb.scala` — Opportunistic Prefetch Read

No 2nd read port is added.

Changes:

- Add sideband IO to accept prefetch read requests
- Regular prediction read always has priority
- Prefetch read is issued only when there is no bank conflict
- Dropped on conflict
- No retry queue

Required logic:

```scala
val prefetchReqValid: Bool
val prefetchReqPc: PrunedAddr
val prefetchAccepted: Bool
val prefetchDroppedByBankConflict: Bool
```

mBTB conflict reduction options:

- Consider increasing `NumInternalBanks`
- Consider improving the banking hash function
- Add align bank/internal bank conflict counters

---

### 5.6 `bpu/history/phr/Phr.scala` — Prefetch Folded History Generation

The PHR currently provides per-stage folded histories.
The prefetch path requires a speculative folded history for TAGE lookup at `PC_{X+3}`.

Changes:

- Apply the `foldedHistoryDelta` read from uBTB to the current `s0_foldedPhr`
- Generate `prefetchFoldedPhr` with per-table folded history correction
- Also generate the hash for `historySignature` computation

Example IO:

```scala
val prefetchDeltaValid: Input(Bool())
val prefetchFoldedHistoryDelta: Input(UInt(FoldedHistoryDeltaWidth.W))
val prefetchFoldedPhr: Output(new PhrAllFoldedHistories(AllFoldedHistoryInfo))
val prefetchHistorySignature: Output(UInt(HistorySignatureWidth.W))  // write key: hash(prefetchFoldedPhr)
val s1HistorySignature: Output(UInt(HistorySignatureWidth.W))        // read key: hash(s1_foldedPhr)
```

Apply method:

```scala
// Per-table slice ranges are determined statically from AllFoldedHistoryInfo
for (i <- 0 until numTageTables) {
  val lo = foldedHistorySliceLo(i)
  val hi = foldedHistorySliceHi(i)
  prefetchFoldedPhr(i) := s0_foldedPhr(i) ^ prefetchFoldedHistoryDelta(hi, lo)
}
```

Notes:

- This is not a simple `shift_and_append(takenBits)` operation.
- Because PHR updates are influenced by `pathHash(cfiPc, target)`, the preferred approach is to precompute per-table folded deltas at commit time and store them.
- The delta XOR computation follows the table order and widths defined in `AllFoldedHistoryInfo`.

---

### 5.7 `bpu/tage/Tage.scala` / `TageTable.scala` — Opportunistic Prefetch Read

No 2nd read port is added.

Changes:

- Add sideband prefetch read request IO
- Regular read has priority
- Prefetch is issued only when there is no table/bank conflict
- Dropped on conflict
- Reduce conflict rate via TAGE table banking expansion or hash improvement

Required logic:

```scala
val prefetchReqValid: Bool
val prefetchReqPc: PrunedAddr
val prefetchFoldedPhr: PhrAllFoldedHistories
val prefetchAccepted: Bool
val prefetchDroppedByBankConflict: Bool
```

Prefetch result assembly:

- Apply TAGE prefetch direction to the branch candidates from the mBTB prefetch result
- SC correction is not applied
- TAGE provider/alt selection logic is applied identically on the prefetch path

---

### 5.8 `bpu/Bpu.scala` — Main Orchestration

#### (a) uBTB aheadInfo wiring

```scala
val s1_aheadValid = ubtb.io.aheadInfo.valid
val s1_aheadPc = ubtb.io.aheadInfo.bits.aheadPc
val s1_foldedHistoryDelta = ubtb.io.aheadInfo.bits.foldedHistoryDelta
// The historySignature stored in the uBTB entry is used only as an optional
// prefetch issue validity filter; it is not the buffer hit key (see (d)(e) below).
```

#### (b) Prefetch request issue

```scala
val s1_prefetchReqValid =
  s1_fire &&
  s1_aheadValid

phr.io.prefetchDeltaValid := s1_prefetchReqValid
phr.io.prefetchFoldedHistoryDelta := s1_foldedHistoryDelta

mbtb.io.prefetchReq.valid := s1_prefetchReqValid
mbtb.io.prefetchReq.pc := s1_aheadPc

tage.io.prefetchReq.valid := s1_prefetchReqValid
tage.io.prefetchReq.pc := s1_aheadPc
tage.io.prefetchReq.foldedPhr := phr.io.prefetchFoldedPhr
```

A prefetch is considered effectively issued only when both mBTB and TAGE accept it conflict-free.

#### (c) Prefetch result assembly

- Select the earliest valid branch candidate from the mBTB prefetch result
- Apply TAGE direction for conditional branches
- Use mBTB target as taken for direct branches
- Skip buffer write for indirect/return
- Convert taken=false to a fallThrough-style prediction

#### (d) Buffer write

The write historySignature is `phr.io.prefetchHistorySignature` computed at prefetch issue (cycle N+2), pipelined to the result-ready cycle (N+4).

```scala
// Computed at prefetch issue: hash(s0_foldedPhr XOR foldedHistoryDelta)
val prefetch_s1_histSig = phr.io.prefetchHistorySignature  // cycle N+2
val prefetch_s2_histSig = RegNext(prefetch_s1_histSig)      // cycle N+3
val prefetch_s3_histSig = RegNext(prefetch_s2_histSig)      // cycle N+4 (write time)

prefetchBuffer.io.writeValid := prefetchResultValid
prefetchBuffer.io.writePc := prefetchPc
prefetchBuffer.io.writeHistorySignature := prefetch_s3_histSig
prefetchBuffer.io.writeData := prefetchPrediction
```

#### (e) Buffer read and S1 prediction replacement

The read historySignature is derived from the actual PHR state at S1 of PC_{X+3} (cycle N+4).
If speculation was correct, write key == read key → hit.

```scala
// s1_foldedPhr is the actual speculative folded history at the current S1 point
val s1_histSig = phr.io.s1HistorySignature  // hash(s1_foldedPhr)

prefetchBuffer.io.readPc := s1_startPc
prefetchBuffer.io.readHistorySignature := s1_histSig

when(prefetchBuffer.io.readHit) {
  s1_prediction := prefetchBuffer.io.readData
}.otherwise {
  s1_prediction := originalS1Prediction
}
```

#### (f) S3 override policy

`s3_override` logic is preserved as-is.

```scala
s3_override := s3_valid && !(s3_prediction === s3_s1Prediction)
```

A buffer hit alone does not suppress override.

#### (g) Flush / invalidate

Buffer flush on redirect is **not required**.

The hit key `{pcTag, historySignature}` acts as a staleness guard. After a redirect, if the actual history diverges, the expectedHistorySignature differs → buffer miss → fallback to the uBTB path. Since S3 override always guarantees correctness, wrong-path entries remaining in the buffer cause no harm.

Explicit flush should be considered only in the following cases:

- Predictor-wide disable via CSR
- Full reset of the large predictor (mBTB/TAGE)

---

### 5.9 `frontend/Bundles.scala` — FtqToBpuIO Extension

Add commit-based threeAhead train channel:

```scala
val aheadTrain: Valid[UbtbAheadTrain] = Valid(new UbtbAheadTrain)
```

Bundle example:

```scala
class UbtbAheadTrain extends BpuBundle {
  val startPc: PrunedAddr = PrunedAddr(VAddrBits)
  val aheadPc: PrunedAddr = PrunedAddr(VAddrBits)
  val foldedHistoryDelta: UInt = UInt(FoldedHistoryDeltaWidth.W)
  val historySignature: UInt = UInt(HistorySignatureWidth.W)
}
```

---

### 5.10 `ftq/Ftq.scala` — Commit Window-based threeAhead Training

The current `io.toBpu.train` is resolve-queue based.
threeAhead metadata must be trained from the committed path, requiring a separate commit-window logic.

Changes:

- Track the `commitPtr ~ commitPtr+3` window on the FTQ commit side
- Generate a train entry when all 4 fetch blocks in the window have committed
- `startPc = entryQueue(commitPtr).startPc`
- `aheadPc = entryQueue(commitPtr + 3).startPc`
- Compute `foldedHistoryDelta` using branch/path information within the window
- Generate `historySignature`
- Forward via `io.toBpu.aheadTrain`

Notes:

- Verify that the commit path contains sufficient branch outcome / path hash information for delta computation.
- If not, add the required information to FTQ entries or a separate side buffer for commit-time delta computation.
- This logic is independent of the existing resolve-based BPU training.

---

### 5.11 Correctness Guard and Validation Counters

Correctness is guaranteed by the existing override path since the S3 final path is preserved.
Counters are needed for performance evaluation and stability validation.

Required counters:

- `prefetchReq`
- `prefetchAccepted`
- `prefetchDroppedByMbtbBankConflict`
- `prefetchDroppedByTageBankConflict`
- `prefetchBufferWrite`
- `prefetchBufferHit`
- `prefetchBufferMiss`
- `prefetchBufferHitSameCycleBypass`
- `prefetchFilteredIndirect`
- `prefetchFilteredReturn`
- `prefetchHitAndS3Match`
- `prefetchHitAndS3Override`
- `prefetchHitButScChanged`
- `prefetchHitButIttageChanged`
- `prefetchHitButRasChanged`

---

### 5.12 Change Scope Summary

| File | Change Type | Difficulty |
| --- | --- | --- |
| `bpu/ubtb/Parameters.scala` | Add threeAhead/history parameters | Low |
| `bpu/ubtb/Bundles.scala` | Add uBTB entry metadata | Medium |
| `bpu/ubtb/MicroBtb.scala` | threeAhead output/train path | Medium |
| `bpu/mbtb/MbtbTagePrefetchBuffer.scala` | New 32-entry CAM buffer | Medium |
| `bpu/mbtb/MainBtb.scala` | Opportunistic prefetch read arbitration | High |
| `bpu/history/phr/Phr.scala` | foldedHistoryDelta application | High |
| `bpu/tage/Tage.scala` / `TageTable.scala` | Opportunistic prefetch read arbitration | High |
| `bpu/Bpu.scala` | Prefetch orchestration and S1 mux | High |
| `frontend/Bundles.scala` | Add aheadTrain IO | Low |
| `ftq/Ftq.scala` | Commit-window threeAhead training | High |

---

## 6. Performance Counters

Section 5.11 lists counters needed for correctness validation. This section extends that list with counters organized by purpose: functionality verification and performance impact measurement. All counters are implemented as CSR-readable saturating 64-bit event counters unless otherwise noted.

---

### 6.1 Functionality Check Counters

These counters verify that each stage of the prefetch pipeline operates as designed. If any stage shows unexpected values (e.g., `pfAccepted` is always zero, or `pfBypassFired` never fires), it indicates a wiring or logic bug rather than a performance problem.

#### Prefetch Issue Pipeline

| Counter | Trigger Condition | Purpose |
| --- | --- | --- |
| `pfReqAttempted` | S1 fires AND `aheadValid` is set | Confirms uBTB threeAhead metadata is being read and prefetch is attempted |
| `pfAcceptedBoth` | mBTB AND TAGE both accept (no conflict) | Verifies opportunistic issue logic works when both paths are free |
| `pfDroppedMbtbConflict` | mBTB bank conflict causes drop | Quantifies mBTB conflict rate impact on coverage |
| `pfDroppedTageConflict` | TAGE bank conflict causes drop | Quantifies TAGE conflict rate impact on coverage |
| `pfDroppedBoth` | Both mBTB and TAGE conflict simultaneously | Separates total drop into causes |

#### Buffer Write Path

| Counter | Trigger Condition | Purpose |
| --- | --- | --- |
| `pfWrittenToBuffer` | Prefetch result valid AND written to buffer | Confirms results are flowing into the CAM |
| `pfFilteredIndirect` | Prefetch result discarded: indirect branch | Validates indirect exclusion policy is active |
| `pfFilteredReturn` | Prefetch result discarded: return instruction | Validates return exclusion policy is active |
| `pfWrittenTaken` | Buffer write with `taken=true` | Distribution of taken vs. not-taken in buffer |
| `pfWrittenNotTaken` | Buffer write with `taken=false` (stored as fallThrough) | Distribution of not-taken predictions stored |

#### Buffer Read and Bypass

| Counter | Trigger Condition | Purpose |
| --- | --- | --- |
| `pfBufferHit` | CAM lookup hits at S1 | Core hit rate numerator |
| `pfBufferMiss` | CAM lookup misses at S1 | Core hit rate denominator complement |
| `pfPcTagHitSigMiss` | pcTag matched but `historySignature` did not match | Validates that the staleness guard is catching diverged-history cases |
| `pfBypassFired` | Same-cycle write-read bypass was triggered | Verifies bypass logic is exercised; if zero, same-cycle write/read never co-occurs |

#### Training Path

| Counter | Trigger Condition | Purpose |
| --- | --- | --- |
| `pfAheadTrainIssued` | FTQ commit side emits `aheadTrain` to uBTB | Confirms commit-window tracking fires |
| `pfUbtbAheadValidOnHit` | uBTB hit AND `aheadValid` is set | Measures training coverage fraction |
| `pfUbtbAheadInvalidOnHit` | uBTB hit AND `aheadValid` is NOT set | Entries without 3-ahead metadata; indicates cold or untrained entries |

#### S3 Correctness Breakdown (when buffer hit)

| Counter | Trigger Condition | Purpose |
| --- | --- | --- |
| `pfHitS3Match` | Buffer hit AND S3 agrees with S1 prediction | Scheme produced a correct result |
| `pfHitS3Override` | Buffer hit AND S3 overrides S1 prediction | Total overrides despite buffer hit |
| `pfHitS3OverrideBySc` | S3 override cause: SC direction flip | Identifies how much SC residual limits the scheme |
| `pfHitS3OverrideByIttage` | S3 override cause: ITTAGE indirect target | Should be rare since indirect is filtered |
| `pfHitS3OverrideByRas` | S3 override cause: RAS return target | Should be rare since return is filtered |
| `pfHitS3OverrideByOther` | S3 override: target/position mismatch not covered above | Catches residual stale-result cases |

---

### 6.2 Performance Check Counters

These counters measure the scheme's impact on frontend efficiency. The primary metric of interest is how many S3 overrides were eliminated and what fraction of overrides the scheme could not address.

#### S3 Override Baseline

| Counter | Trigger Condition | Purpose |
| --- | --- | --- |
| `s3OverrideTotal` | S3 override fires (any cause) | Baseline reference; compare before and after enabling the scheme |
| `s3OverrideConditional` | S3 override AND branch is conditional | Isolates the portion the scheme can target |
| `s3OverrideDirect` | S3 override AND branch is direct | Isolates the portion the scheme can target |
| `s3OverrideIndirect` | S3 override AND branch is indirect | Portion permanently delegated to ITTAGE |
| `s3OverrideReturn` | S3 override AND branch is return | Portion permanently delegated to RAS |

#### Scheme Opportunity and Outcome

| Counter | Trigger Condition | Purpose |
| --- | --- | --- |
| `s3OverrideWithPfHit` | S3 override fires AND buffer was hit at this S1 | Overrides that the scheme attempted but could not prevent (= `pfHitS3Override`) |
| `s3OverrideWithPfMiss` | S3 override fires AND buffer was missed at this S1 | Overrides where the scheme had no result available |
| `s3OverrideWithPfDrop` | S3 override fires AND prefetch was dropped (bank conflict) | Overrides the scheme could not address due to bank conflict |
| `pfEffectiveOverridePrevented` | Buffer hit AND S3 agreed (= `pfHitS3Match`) | Overrides effectively eliminated by the scheme |

Derived metrics (computed offline from counter pairs):

| Metric | Formula | Interpretation |
| --- | --- | --- |
| `pfAcceptRate` | `pfAcceptedBoth / pfReqAttempted` | Bank conflict impact; target ≥ 70% |
| `pfHitRate` | `pfBufferHit / pfWrittenToBuffer` | Lookahead path accuracy; measures how often aheadPc was correct |
| `pfSignatureGuardRate` | `pfPcTagHitSigMiss / (pfBufferHit + pfPcTagHitSigMiss)` | Fraction of pcTag matches caught by signature guard |
| `pfPrecision` | `pfHitS3Match / pfBufferHit` | Fraction of buffer hits that produced a correct S1 prediction |
| `pfOverrideReductionRate` | `pfEffectiveOverridePrevented / s3OverrideTotal` | Scheme's overall contribution to override reduction |
| `pfUncoverableRate` | `(s3OverrideIndirect + s3OverrideReturn) / s3OverrideTotal` | Override fraction permanently outside scheme scope |
| `pfBankConflictLoss` | `s3OverrideWithPfDrop / s3OverrideTotal` | Override fraction lost to bank conflict (motivation for NumInternalBanks increase) |

#### Buffer Sizing Indicators

| Counter | Trigger Condition | Purpose |
| --- | --- | --- |
| `pfBufferEviction` | PLRU/round-robin evicts a valid entry | If high relative to `pfBufferHit`, 32 entries may be insufficient |
| `pfBufferEvictionBeforeUse` | Entry evicted while still valid and never read | Direct indicator that buffer capacity is a bottleneck |

---

## 7. Open Questions

1. Does the FTQ commit side carry sufficient branch outcome / path hash information for delta computation?
2. Can timing closure be achieved with a 32-entry CAM in the S1 path, including the same-cycle bypass?
3. How much does the mBTB/TAGE bank conflict rate affect effective scheme coverage? Measure per-workload conflict rates via simulation before implementation; if the prefetchAccepted rate falls below a target (e.g., 70%), prioritize increasing `NumInternalBanks`.
4. Does the same-cycle write-read bypass timing close together with the S1 mux?
5. How much does the indirect/return exclusion policy limit the actual reduction in override frequency?

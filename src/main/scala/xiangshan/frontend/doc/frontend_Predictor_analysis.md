# frontend_Predictor_analysis.md

- Block: Frontend
- Module: Predictor (BPU)
- Source: BPU.scala, Composer.scala, FauFTB.scala, FTB.scala, Tage.scala, SC.scala, ITTAGE.scala, RAS.scala, Bim.scala
- Protocols: Decoupled (bpu_to_ftq), Valid (redirect/update)
- Key Params: numDup=4, HistoryLength, numBr, FtqSize, MaxMetaLength
- Last updated: 2026-02-18

→ See [frontend_block_diagram_overview.md](./frontend_block_diagram_overview.md)
→ See [frontend_in_out_seq_diagram.md](./frontend_in_out_seq_diagram.md#group-b)

---

## 1. Module Summary

- **Role**: 3-stage branch prediction pipeline. It consists of S1 (FauFTB), S2 (Main FTB+TAGE base), and S3 (TAGE+SC+ITTAGE+RAS). Each stage transmits predictions to FTQ, and when subsequent stages make more precise predictions, FTQ is overridden (redirected).
- **Position**: `Frontend.scala` → `Module(new Predictor)` (inside FrontendInlinedImp)
- **Number of pipeline stages**: 3 register stages (S1/S2/S3), S0 is combinational launch phase

---

## 2. Key Parameters

| Parameter | Source | Default | Impact |
| ------------- | ---------------------------------- | ------- | ------------------------------------- |
| numDup | `HasBPUConst.numDup` | 4 | Number of PC/history replicas (for timing closure) |
| HistoryLength | `p(XSCoreParamsKey).HistoryLength` | 256 | Global branch history length (GHR size) |
| numBr | `p(XSCoreParamsKey).numBr` | 2 | Number of branch slots per FTB entry |
| numBrSlot | `numBr - 1` | 1 | Number of dedicated branch slots |
| totalSlot | `numBr` | 2 | total slots (brSlots + tailSlot) |
| MaxMetaLength | `HasBPUConst.MaxMetaLength` | 512 | Predictive metadata storage bit count |
| FtqSize | `p(XSCoreParamsKey).FtqSize` | 64 | FTQ size (based on BPU stall) |
| numBpStages | 3 | 3 | Number of pipeline stages |

---

## 3. Interfaces

| Port              | Dir | Bitwidth | Protocol  | Description                            |
| ----------------- | --- | -------- | --------- | -------------------------------------- |
| `io.bpu_to_ftq.resp` | out | BpuToFtqBundle | Decoupled | Prediction result → FTQ (including s1/s2/s3) |
| `io.ftq_to_bpu.redirect` | in | BranchPredictionRedirect | Valid | FTQ→BPU redirect (misprediction) |
| `io.ftq_to_bpu.update` | in | BranchPredictionUpdate | Valid | FTQ→BPU training data |
| `io.ftq_to_bpu.enq_ptr` | in | FtqPtr | — | FTQ current enqueue pointer (stall judgment) |
| `io.ctrl` | in | BPUCtrl | — | Each predictor enable/disable |
| `io.reset_vector` | in | PAddrBits | — | Reset PC |

**BPUCtrl field**: `ubtb_enable`, `btb_enable`, `bim_enable`, `tage_enable`, `sc_enable`, `ras_enable`, `loop_enable`

---

## 4. Internal Pipeline / State

### 4.1 Stage composition

> **Key**: All predictors receive `s0_startPc` **at the same time**. (`p.io.startPc := s0_startPc` — `Bpu.scala:192`)
> The reason the output comes out of S1/S2/S3 is because of the difference in **lookup latency** of each predictor, and each stage does not process different PCs.

```
S0 (combinational)
  ├─ s0_startPc: MuxCase
  │    priority: redirect.target > s3_override→s3_prediction.target > s1_valid→s1_prediction.target > s0_startPcReg
├─ s0_startPcReg: RegEnable(s0_startPc, !s0_stall) — Maintain PC when stalling
  ├─ s0_stall: !(s1_valid || s3_override || redirect.valid)
  ├─ s0_fire: s1_ready && all_predictors_reset_done
└─ → Pass s0_startPc to all predictors (simultaneously, parallel)

S1 (REG: s1_valid, s1_startPc = RegEnable(s0_startPc, s0_fire))
├─ [1-cycle lookup result] ubtb → 1 candidate (always taken, index 0)
├─ [1-cycle lookup result] abtb → maximum 8 candidates (index 1..8)
│ └─ Concat the two outputs: s1_btbPrediction = VecInit(ubtb.io.prediction) ++ abtb.io.prediction (9 pools in total)
│ ※ Two predictors do not compete — 9 candidates are combined into one Vec and sorted by position
├─ [1-cycle lookup result] utage → conditional branch direction correction (s1_utageHitMask: override on the same cfiPosition candidate)
├─ [1-cycle lookup result] uras → ret target provided (s1_isRet && uras.specOut.isCanUse → s1_prediction.target)
  ├─ [always on]          fallThrough → sequential fallthrough prediction (fallback)
├─ s1_takenMask: Calculate whether taken for each of the 9 (conditional → utage or BTB direction, direct/indirect → always taken)
├─ s1_firstTakenBranchOH: Select at least 1 cfiPosition among candidates with takenMask=1 using CompareMatrix
│ └─ s1_firstTakenBranchOH(0)=1 → uBTB wins / (0)=0 → 1 of ABTB candidates wins
├─ s1_prediction: Selected single candidate (or fallThrough) → .target is next cycle S0 PC
  ├─ s1_fire = s1_valid && s2_ready && prediction.ready
└─ if s1_fire → prediction.valid=1, s3Override=0 → FTQ (transfer s1_startPc, s1_prediction)

S2 (REG: s2_valid, s2_startPc = RegEnable(s1_startPc, s1_fire))  — same PC as S1
├─ [2-cycle lookup result] mbtb → s2_mbtbResult (main BTB, more accurate branch information)
├─ [2-cycle lookup result] tage → tage.io.prediction (TAGE table direction prediction)
├─ [2-cycle lookup result] sc → sc.io.scTakenMask (Statistical Corrector correction)
├─ s2_takenMask: Apply tage/sc direction results to mbtb entries
  ├─ s2_fire = s2_valid && s3_ready
├─ s2_flush = s3_flush || s3_override ← BPU internal flush (not passed to FTQ)
└─ [FTQ not delivered] S2 results are only latched to S3, not sent directly to FTQ

S3 (REG: s3_valid, s3_startPc = RegEnable(s2_startPc, s2_fire))  — same PC as S1/S2
├─ s3_mbtbResult = RegEnable(s2_mbtbResult, s2_fire) — S2 result latch
  ├─ s3_takenMask  = RegEnable(s2_takenMask, s2_fire)
├─ [3-cycle lookup result] ittage → ittage.io.prediction.target (indirect branch target)
├─ [3-cycle lookup result] ras → ras.io.topRetAddr (full RAS ret address)
  ├─ s3_prediction.target: MuxCase(fallThrough, [(taken&&useRas)→ras, (taken&&useIttage)→ittage, taken→mbtb])
├─ s3_s1Prediction = RegEnable(RegEnable(s1_prediction, s1_fire), s2_fire) — S1 prediction propagation
  ├─ s3_override = s3_valid && !(s3_prediction === s3_s1Prediction)
├─ s3_fire = s3_valid (always fire — result always consumed)
└─ if s3_override → prediction.valid=1, s3Override=1 → FTQ (transfer s3_startPc, s3_prediction)
```

### 4.2 History Register Management

- `phr` (Phr module): Path History Register — Folded versions such as `s0_foldedPhr`, `s1_foldedPhr` provided
- `commonHR` (CommonHR module): Global History Register — provided by `s0_commonHR`
- commonHR update during S3 fire: `startPc`, `target`, `taken`, `firstTakenBranch`, branch position

### 4.3 FTQ forwarding structure (BpuToFtqIO)

```
prediction: DecoupledIO[BpuPrediction]
├─ startPc: PrunedAddr — s1_startPc or s3_startPc (when s3Override)
├─ target: PrunedAddr — predicted next PC
  ├─ taken:   Bool
  ├─ cfiPosition: UInt
  ├─ attribute: CfiAttribute   — isDirect/isIndirect/isConditional/isReturn
├─ s3Override: Bool — If true, FTQ updates existing entry (override), if false, new enq
  └─ (via fromStage())

s3FtqPtr: FtqPtr — override target entry index (RegEnable level 2)
meta: Valid[BpuMeta] — s3_valid when: redirectMeta / resolveMeta / commitMeta
```

---

## 5. Functionality

### 5.1 PC Creation (S0)

- `s0_startPc` MuxCase priority (lowest to highest):
1. `redirect.valid` → `redirect.bits.target` (backend mispred recovery)
  2. `s3_override` → `s3_prediction.target` (S3 override)
3. `s1_valid` → `s1_prediction.target` (fetch next block with S1 prediction target)
4. else → `s0_startPcReg` (stall: maintain previous PC)
- `s0_stall = !(s1_valid || s3_override || redirect.valid)` → When there is no valid PC source

### 5.2 S1 prediction assembly (1-cycle latency predictors)

**Candidate Pooling (Bpu.scala:266)**:
```
s1_btbPrediction = VecInit(ubtb.io.prediction) ++ abtb.io.prediction
// ↑ Up to 1 uBTB (index 0) ↑ Up to 8 ABTB (index 1..8)
// → Concat a total of up to 9 candidates into one Vec

// uBTB: tag = fetch block start PC[22:1] → fully-assoc comparison → maximum 1 hit
// (32 entries × 1 entry/fetch block → maximum 1 entry for the same fetch block → max 1 hit)
// ABTB: tag = fetch block start PC[24:1] → Multiple branches within the same fetch block are stored in each way
// → Multiple ways can hit simultaneously → Up to 8 Valid[Prediction] outputs
// Combine the outputs of two predictors into one candidate pool and determine a single prediction by comparing positions.
```

**takenMask calculation (Bpu.scala:270-278)**:
- `s1_utageHitMask[i]`: If cfiPosition of utage matches candidate i → use utage direction
- `s1_takenMask[i] = pred[i].valid && (isDirect || isIndirect || (isConditional && Mux(utageHit, utageTaken, pred.taken)))`

**Select single prediction (Bpu.scala:299-304)**:
```
s1_compareMatrix = CompareMatrix(s1_btbPrediction[*].cfiPosition) // Pairwise comparison of 9 positions
s1_firstTakenBranchOH = compareMatrix.getLeastElementOH(s1_takenMask) // Minimum position among taken → 1-hot
s1_firstTakenBranch = Mux1H(s1_firstTakenBranchOH, s1_btbPrediction) // 1 selected candidate

// Determine the winner (debug signal, Bpu.scala:311-314):
// s1_firstTakenBranchOH(0) = 1 → uBTB candidate wins
// s1_firstTakenBranchOH(0) = 0 → 1 of the ABTB candidates wins (smaller position)
// If the position is the same, index 0 (uBTB) takes precedence (OHToUInt characteristic)
```

- `s1_prediction = Mux(s1_taken, s1_firstTakenBranch.bits, fallThrough.io.prediction)`
- Upon ret branch: `s1_prediction.target := uras.io.specOut.retTarget` (fast RAS)
- `s1_prediction.target` → next cycle `s0_startPc` (Bpu.scala:439: `s1_valid → s1_prediction.target`)

### 5.3 S2 prediction assembly (2-cycle latency predictors)

- `s2_mbtbResult = mbtb.io.result` — main BTB result (lookup to S0 PC, result after 2 cycles)
- `s2_condTakenMask`: Apply tage/sc direction to mbtb conditional branch
- `sc.io.scUsed` → If SC is used, `sc.io.scTakenMask` takes precedence
- `tage.useProvider` → use provider table prediction
- else → default direction of mbtb
- `s2_takenMask = s2_condTakenMask || s2_jumpMask` (direct/indirect is always taken)
- Not delivered to FTQ — only delivered to S3

### 5.4 S3 prediction assembly and override (3-cycle latency predictors)

- `s3_mbtbResult = RegEnable(s2_mbtbResult, s2_fire)` — S2 mbtb result latch
- `s3_useIttage = s3_firstTakenBranch.bits.attribute.needIttage && ittage.io.prediction.hit`
- `s3_useRas = s3_firstTakenBranch.bits.attribute.isReturn`
- `s3_prediction.target` priority: ras.topRetAddr > ittage.prediction.target > mbtb.target > fallThrough
- `s3_s1Prediction = RegEnable(RegEnable(s1_prediction, s1_fire), s2_fire)` — S1 prediction propagation from the same PC
- `s3_override = s3_valid && !(s3_prediction === s3_s1Prediction)` — when the two predictions are different

### 5.5 FTQ delivery path

```scala
// Bpu.scala:415-421
io.toFtq.prediction.valid := s1_valid && s2_ready || s3_override
when(s3_override) {
io.toFtq.prediction.bits.fromStage(s3_startPc, s3_prediction) // override path
}.otherwise {
io.toFtq.prediction.bits.fromStage(s1_startPc, s1_prediction) // Normal path
}
io.toFtq.prediction.bits.s3Override := s3_override
```

- **Normal path**: s1_fire → s1_prediction to new enq to FTQ (s3Override=0)
- **override path**: s3_override → Update existing entry (s3FtqPtr) with s3_prediction (s3Override=1), prediction.ready independent

---

## 6. Flow / Backpressure Control

| Conditions | Action | Code Basis |
| ---- | ---- | --------- |
| FTQ full (`prediction.ready=0`) | `s1_fire=0` → S1 stall, S0 stall chain | `s1_fire := s1_valid && s2_ready && prediction.ready` |
| s3_override occurs | `s2_flush=1`, `s1_flush=1` → BPU internal S1+S2 in-flight kill (following PCs) | `s2_flush := s3_flush || s3_override` |
| redirect.valid | `s3_flush=1` → s3_valid clear, s2_flush, s1_flush chain | `s3_flush := redirect.valid` |
| s0_stall | Keep `s0_startPcReg` (freeze PC), freeze predictors input | `s0_stall := !(s1_valid \|\| s3_override \|\| redirect.valid)` |
| s3_override + FTQ full | s3_override updates FTQ regardless of prediction.ready with `bpuS3Redirect` | `Ftq.scala: bpuS3Redirect = prediction.valid && s3Override` |

---

## 7. Error / Exception Handling

- BPU itself does not generate exceptions.
- Misprediction detection: FTQ compares ROB commit results and sends `toBpu.redirect.valid`
- `BranchPredictionUpdate.mispred_mask[numBr+1]`: Indicates which slot is incorrect
- `false_hit`: It was an FTB hit, but it was actually a different entry (ghost entry, etc.)
- rollback speculatively updated GHR when redirect is received: restore `ghistPtrGen` priority

---

## 8. Timing Hints

| Critical Path | Description |
| ------------- | ---- |
| S0 PC mux | `s0_startPc` MuxCase: redirect/s3_override/s1_valid/hold Level 4 Priority — Timing Critical |
| S1 ubtb/abtb lookup | ubtb: fully-associative tag comparison; abtb: ahead fetch + tag comparison (parallel) |
| S1 s1_prediction assembly | CompareMatrix(position): Select minimum cfiPosition — wide comparator |
| S2 mbtb SRAM | main BTB SRAM read latency + tag comparison |
| S2 tage/sc | Simultaneous lookup of multiple TAGE tables (different history lengths), SC application |
| S3 ittage/ras | indirect target table lookup (ittage), RAS top read (ras) |
| phr folded history | folded path history Multi-stage XOR tree — compLen bits all |
| s3_override decision | `s3_prediction === s3_s1Prediction`: Compare multiple fields across — wide equality |

---

## 9. Pseudocode

> **Caution**: All three predictor groups receive the same `s0_startPc` input.
> The result in S1/S2/S3 is due to the **lookup latency difference** of each predictor,
> S2 is a more accurate result for the same s0_startPc** rather than the next PC in S1.

```
=== Cycle T: S0 ===
s0_startPc = MuxCase(s0_startPcReg, [
  redirect.valid  → redirect.bits.target,
  s3_override     → s3_prediction.target,
  s1_valid        → s1_prediction.target
])
s0_stall = !(s1_valid || s3_override || redirect.valid)
if not s0_stall:
  s0_startPcReg := s0_startPc

//Simultaneous delivery of s0_startPc to all predictors (start lookup)
for p in [ubtb, abtb, utage, uras, fallThrough, mbtb, tage, sc, ittage, ras]:
  p.io.startPc := s0_startPc

s0_fire = s1_ready && all_predictors_reset_done


=== Cycle T+1: S1 === (Receive 1-cycle latency result for s0_startPc)
if s0_fire:
s1_startPc := RegEnable(s0_startPc) // = s0_startPc of S0 above
  s1_valid   := true

// 1-cycle lookup result (based on s0_startPc):
// uBTB (1) + ABTB (maximum 8) → concat with a total of 9 candidate pools (not competition)
s1_btbPrediction = VecInit(ubtb.io.prediction) ++ abtb.io.prediction  // index 0=uBTB, 1..8=ABTB
s1_utageHitMask  = [utage.prediction.cfiPosition == pred.cfiPosition for pred in btbPred]
s1_takenMask = [pred.valid && (direct || indirect || (conditional && Mux(utageHit, utageTaken, pred.taken)))]
// CompareMatrix: Select minimum cfiPosition among 9 candidates with takenMask=1 → single s1_prediction
s1_firstTakenBranchOH = CompareMatrix(positions).getLeastElementOH(takenMask)
s1_prediction = Mux(s1_taken, firstTakenBranch.bits, fallThrough.io.prediction)
// → s1_prediction.target determines s0_startPc for the next cycle (s1_valid path)
if s1_prediction.attribute.isReturn && uras.specOut.isCanUse:
  s1_prediction.target := uras.io.specOut.retTarget

s1_fire = s1_valid && s2_ready && prediction.ready
if s1_fire:
// Forward to FTQ (new enq, s3Override=0)
  prediction.valid = 1
  prediction.bits  = fromStage(s1_startPc, s1_prediction)
  prediction.bits.s3Override = 0
// s0_startPc next cycle: use s1_prediction.target as next block PC

if s1_flush (= s2_flush):
  s1_valid := false


=== Cycle T+2: S2 === (Receive 2-cycle latency results for s0_startPc)
if s1_fire:
s2_startPc := RegEnable(s1_startPc) // = still original s0_startPc
  s2_valid   := true

// 2-cycle lookup result (based on s0_startPc):
s2_mbtbResult = mbtb.io.result // main BTB (more accurate than ubtb/abtb)
s2_condTakenMask = Apply tage/sc direction to mbtb entries:
  Mux(sc.scUsed → sc.scTaken, tage.useProvider → tage.providerPred, else → mbtb.taken)
s2_takenMask = s2_condTakenMask || jumpMask

// No direct delivery to FTQ — only to S3
s2_fire = s2_valid && s3_ready
if s2_flush (= s3_flush || s3_override):
  s2_valid := false


=== Cycle T+3: S3 === (Receive 3-cycle latency results for s0_startPc)
if s2_fire:
s3_startPc := RegEnable(s2_startPc) // = still original s0_startPc
  s3_valid   := true

// S2 result latch (pipeline forward to S3):
s3_mbtbResult        = RegEnable(s2_mbtbResult, s2_fire)
s3_takenMask         = RegEnable(s2_takenMask, s2_fire)
s3_firstTakenBranch  = Mux1H(firstTakenBranchOH, s3_mbtbResult)
s3_s1Prediction = RegEnable(RegEnable(s1_prediction, s1_fire), s2_fire) // S1 prediction of the same PC

// 3-cycle lookup result (based on s0_startPc):
s3_useRas    = s3_firstTakenBranch.attribute.isReturn
s3_useIttage = s3_firstTakenBranch.attribute.needIttage && ittage.prediction.hit
s3_prediction.target = MuxCase(fallThrough.target, [
  (taken && useRas)    → ras.io.topRetAddr,
  (taken && useIttage) → ittage.io.prediction.target,
  taken                → s3_firstTakenBranch.bits.target
])

// Compare with s1 prediction (for same s0_startPc)
s3_override = s3_valid && !(s3_prediction === s3_s1Prediction)
if s3_override:
// Forward to FTQ (override, s3Override=1) — prediction.ready is irrelevant
  prediction.valid = 1
  prediction.bits  = fromStage(s3_startPc, s3_prediction)
  prediction.bits.s3Override = 1
io.toFtq.s3FtqPtr = s3_ftqPtr // override target entry index
// Inside BPU: Clear in-flight for the following PCs
s2_flush := 1 // s1_flush also = s2_flush
s0_startPc Next cycle: s3_prediction.target (second-rank MUX)

// s3_fire = s3_valid (always)
if s3_valid:
  commonHR.update(s3_startPc, s3_prediction)
  ras.specIn := {s3_startPc, s3_prediction}

s3_fire = s3_valid // always fire


=== on redirect (backend mispred) ===
s3_flush := redirect.valid → clear s3/s2/s1_valid
phr rollback using redirect.bits meta
ras rollback using redirect.bits meta
s0_startPc priority: redirect.bits.target
```

---

### 9.1 Conditional Branch Prediction (mBTB + TAGE + SC)

```
=== [Case 1: Conditional Branch] mBTB + TAGE + SC ===
// Core: only determines direction(taken/not-taken). The target is fixed to mBTB.

// --- S0: SRAM read requests (simultaneous sending) ---
mbtb.readReq(alignBankIdx=PC[5], internalBankIdx=PC[7:6], setIdx=PC[15:8])
for t in tage.tables[0..7]:
  t.readReq(setIdx = fold(PC XOR pathHist XOR globalHist, t.histLen),
            tag    = fold(PC XOR globalHist, TagWidth))
for t in sc.tables:  // path-based + global-based
  t.readReq(setIdx = fold(pathHist XOR PC, t.histLen))

// --- S1: SRAM responses arrive ---
mbtb_s1_rawEntries = mbtb.readResp() // tag compare in S2
tage_s1_rawResps   = tage.tables.readResp()
sc_s1_rawResps     = sc.tables.readResp()

// --- S2: tag compare + direction decision ---

// [mBTB] tag compare
for each way i:
  mbtb_hit[i]   = entry[i].valid && entry[i].tag == PC[31:16]
  mbtb_taken[i] = mbtb.counterSram[i].isPositive  // 2-bit saturating counter

// [TAGE] Select provider (hit table with longest history)
for each way i: // = based on mBTB result slot
  hitTableMask    = [tage.tables[j].tag == computedTag[j] for j in 0..7]
  providerTableOH = getLongestHistTableOH(hitTableMask)
  provider        = tage.tables[providerTableOH]
  hasAlt          = any hit besides provider
  alt             = tage.tables[altTableOH]        // second longest hit

  useProvider  = hasProvider && !(useAltOnNa && provider.takenCtr.isWeak)
  providerPred = provider.takenCtr.isPositive      // MSB of 2-bit ctr
  altPred      = alt.takenCtr.isPositive

// [SC] adaptive-threshold confidence check
for each way i:
  percsum[j] = sc.tables[j].ctr * 2 + 1           // getPercsum (signed partial sum)
  scSum      = sum(percsum for all SC tables)

// Adjust threshold according to TAGE provider confidence
tageConfHigh = provider.takenCtr.isSaturate // strong confidence (both ends)
tageConfMid = provider.takenCtr.isMid // middle
  // tageConfLow  = otherwise

  adaptiveThres = scThreshold >> (1 if confHigh else 2 if confMid else 3)

  scUsed[i]  = mbtb_hit[i] && hasProvider && aboveThreshold(|scSum|, adaptiveThres)
scTaken[i] = scSum >= 0 // direction determined by sign

// [Final direction] Priority: SC > TAGE provider > TAGE alt > mBTB counter
for each way i:
  condTaken[i] = mbtb_hit[i] && isConditional[i] &&
MuxCase(mbtb_taken[i], // Default: mBTB 2-bit counter
scUsed[i] → scTaken[i], // SC active: scSum sign used
      useProvider → providerPred, // TAGE provider hit
      hasAlt      → altPred       // TAGE alt hit
    )

// --- S3: first taken selection + target + override ---
takenMask[i]     = condTaken[i] || jumpTaken[i]   // jumpTaken = isDirect||isIndirect
firstTakenOH     = CompareMatrix(positions).getLeastElementOH(takenMask)
firstTakenBranch = Mux1H(firstTakenOH, s3_mbtbResult)

s3_prediction.taken       = takenMask.reduce(||)
s3_prediction.cfiPosition = firstTakenBranch.bits.cfiPosition
s3_prediction.target      = reconstruct(PC, firstTakenBranch.bits.targetLowerBits,
                                             firstTakenBranch.bits.targetCarry)
// conditional branch target only in mBTB — TAGE/SC only corrects direction

s3_override = s3_valid && (s3_prediction != s3_s1Prediction)
```

---

### 9.2 Indirect Jump prediction (mBTB + ITTage, non-return)

```
=== [Case 2: Indirect Jump] mBTB + ITTage (isReturn=false) ===
// Key point: direction is always taken. Target accuracy is the only issue.

// --- S0: SRAM read requests ---
mbtb.readReq(alignBankIdx=PC[5], internalBankIdx=PC[7:6], setIdx=PC[15:8])
// ITTage is not read from S0 (power opt: wait for s1_isIndirect)
// ※ In theory, S0 speculative read → prediction can be completed in S2, but not currently adopted.

// --- S1: mBTB SRAM resp + ITTage read req ---
mbtb_s1_rawEntries = mbtb.readResp()
s1_isIndirect = firstTakenBranch.attribute.needIttage // abtb/ubtb s1 result based

if s1_isIndirect:
  for t in ittage.tables:
    t.readReq(setIdx = fold(hist XOR PC, t.histLen),
              tag    = fold(hist XOR PC, TagWidth))

// --- S2: mBTB tag compare + ITTage SRAM resp + provider selection ---

// [mBTB] tag compare
for each way i:
  mbtb_hit[i]    = entry[i].valid && entry[i].tag == PC[31:16]
  mbtb_target[i] = reconstruct(PC, entry[i].targetLowerBits, entry[i].targetCarry)
jumpTaken[i] = mbtb_hit[i] && isIndirect // always taken (no direction prediction required)

// Select [ITTage] provider (hit table with longest history)
ittage_hitMask    = [ittage.tables[j].tag == computedTag[j] for j in tables]
ittage_providerOH = getLongestHistTableOH(ittage_hitMask)
ittage_provided   = any(ittage_hitMask)
ittage_target = ittage.tables[ittage_providerOH].target // Save entire target

// s2_ittageTarget latch → output from S3 to io.prediction.target

// --- S3: select target + override ---
firstTakenBranch = Mux1H(firstTakenOH, s3_mbtbResult)

// Priority: RAS(isReturn) > ITTage(needIttage && hit) > mBTB target
s3_useRas    = firstTakenBranch.bits.attribute.isReturn
s3_useIttage = firstTakenBranch.bits.attribute.needIttage && ittage.prediction.hit

s3_prediction.taken = true // indirect is always taken
s3_prediction.target = MuxCase(firstTakenBranch.bits.target,  // fallback: mBTB
  (s3_taken && s3_useRas)    → ras.topRetAddr,               // return: RAS
  (s3_taken && s3_useIttage) → ittage.prediction.target      // indirect: ITTage
)
// ※ mBTB entry exists even when isReturn (attribute=Return) → target is overridden by RAS

s3_override = s3_valid && (s3_prediction != s3_s1Prediction)
```

---

## 10. Notes / Assumptions

- **Same PC input**: All predictors share `s0_startPc` — `Bpu.scala:192 p.io.startPc := s0_startPc`
- **S1 prediction = fast path**: ubtb+abtb+utage+uras result. Since it is 1 cycle, FTQ enq and next block fetch start immediately.
- **S3 override condition**: `!(s3_prediction === s3_s1Prediction)` — Occurs only when the S3 result is different from S1 for the same PC (target, taken, cfiPosition, attribute all compared)
- **S2 is not delivered directly to FTQ**: S2 mbtb/tage/sc results are propagated to S3 stage and used to assemble s3_prediction. Different from previous architecture (FauFTB→FTB s2_redirect)
- **phr (Phr), commonHR (CommonHR)**: Replaces GHR/FoldedHistory in previous architecture. separate management of path history + global history
- **uras (MicroRas)**: Provides fast ret target in S1. specOut of full RAS (ras) is used in S3
- **fastTrain**: Generate `BpuFastTrain` signal when `s3_valid` → Used for abtb fast training (`Bpu.scala:183-188`)

---

## 11. BTB/FTB hierarchy details

### 11.1 Structure Overview

This version (KunMingHu v3) has evolved from the existing `uFTB → FTB` two-tier structure to a **uBTB → ABTB → MBTB** three-tier BTB layer.
Each BTB produces results at a different stage and is paired with a different direction predictor.

```
S1 (1-cycle): uBTB + ABTB ──→ uTAGE (direction correction)
S2 (2-cycle): MBTB ──────────→ TAGE(8-table) + SC (direction correction)
S3 (3-cycle): (no BTB) ───→ ITTAGE (indirect target) + RAS (return target)
```

**BTB method classification**:

Common BTB designs fall into two categories:

| method | tag criteria | Action |
|---|---|---|
| **Block BTB** (fetch-block indexed) | **fetch block start PC** | 1 entry covers 1 fetch block → 1 lookup returns prediction of the first taken branch of the corresponding fetch block |
| **Individual BTB** (branch-indexed) | **Individual branch instruction PC** | 1 entry covers 1 branch → If there are N branches in one fetch block, N lookups are required |

**uBTB / ABTB / MBTB in this implementation are all Block BTB**:

| BTB | tag unit | Number of returns per lookup |
|---|---|---|
| **uBTB** | 64B fetch block (PC[22:1]) | **Maximum 1** (32 entries fully-assoc, maximum 1 entry for the same fetch block) |
| **ABTB** | 64B fetch block (PC[24:1]) | **Up to 8** (8 ways sharing the same tag, storing multiple branches of one fetch block) |
| **MBTB** | 32B half-block (tag=PC[31:16]) | **Up to 8** (2 AlignBanks × 4 ways, storing multiple branches in each half-block) |

---

### 11.2 uBTB (Micro BTB) — S1 Stage

| Item | value |
|---|---|
| BTB method | **Block BTB** — tag = fetch block start PC[22:1], 1 entry per fetch block, 1 taken branch per entry |
| structure | **Fully Associative Cache** |
| Number of entries | **32** |
| Tag Width | **22 bits** |
| Target Width | 22 bits (2B aligned) |
| Useful Counter | 2 bits |
| Replacer | **PLRU** (least-useful first) |
| Use History | **None** |
| Predicted Latency | **1 cycle** |

**AddrField bit structure** (`ubtb/Helpers.scala:25`, `ubtb/Parameters.scala`):
```
PC bit : [ 0  ] [    22:1    ] [ VAddrBits-1:23 ]
field  :  inst       tag           unused
          Off    (22 bits)
```

| field | PC bits | Remarks |
|---|---|---|
| instOffset | PC[0:0] | 1 bit, always 0 (RVC 2B alignment) |
| tag | **PC[22:1]** | 22 bits, used for branch prediction |
| targetLower (extraField) | **PC[22:1]** | 22 bits (same location as tag, for storing destination address) |

**Lookup method** (`MicroBtb.scala:72`):
```
// Simultaneous parallel comparison of all 32 entries (fully associative)
s1_tag   = PC[22:1]
s1_hitOH = entries.map(e => e.valid && e.tag === s1_tag)

// When hit: always-taken predictor
prediction.taken       := s1_hit
prediction.cfiPosition := hitEntry.slot1.position
prediction.target      := reconstruct(startPc, hitEntry.slot1.target)
```

> **Feature**: No set index. Direct comparison of all tags. The upper PC bits (PC[VAddrBits-1:23]) are ignored, so aliasing is possible when the address space is wide.

**Entry Contents** (`ubtb/Bundles.scala:32`):
```
MicroBtbEntry {
tag: UInt(22) // PC[22:1], for tag comparison
usefulCnt: SatCtr(2-bit, signed) // useful: SaturateNegative = invalid

slot1 { // main predicted branch (taken candidate)
position: UInt(5) // Branch position in fetch block (instruction slot based on 32B-align)
    attribute:      BranchAttribute  // branchType(2) + rasAction(2) = 4 bits
target: UInt(22) // partial target: branch target [22:1] on PC
isStaticTarget: Bool // Only the same target was observed (for aliasing detection)
  }
slot2 { // TODO: 2-taken — Candidate for the second taken branch in the fetch block (currently always valid=false, unused)
    position:  UInt(5)
    attribute: BranchAttribute
    target:    UInt(22)
valid: Bool // Whether slot2 is valid (currently always false)
taken: Bool // slot2 predicted direction (currently not referenced)
  }
}
```

**Common Type Definition** (`bpu/Bundles.scala`):
```
BranchAttribute (4-bit) {
  branchType: 2-bit { None=0, Conditional=1, Direct=2, Indirect=3 }
  rasAction:  2-bit { None=0, Pop=1(ret), Push=2(call), PopAndPush=3 }
  // needIttage = isIndirect && !hasPop
}

TargetCarry (2-bit) { // For partial target boundary correction
Fit=0 : target upper == startPc upper (no carry)
Overflow=1: Target moves to upper area → upper + 1
Underflow=2: target is lower area → upper - 1
// uBTB: EnableTargetFix=false → TargetCarry not saved, startPc upper used as is
// MBTB: always save
}

CfiPosition (5-bit): 32B-align standard instruction slot index (0..31 / 2B = maximum 16 insts per 32B)
```

**Next PC Decision** (`MicroBtb.scala:79`):
```
if (s1_hit):
  nextPc.taken       = true           // always-taken
  nextPc.cfiPosition = slot1.position
  nextPc.attribute   = slot1.attribute
// partial target → full VAddr reconstruction
  nextPc.target = Cat(
startPc[VAddrBits-1:23], // upper: EnableTargetFix=false, so startPc stays on top
    slot1.target[21:0],          // 22-bit partial target (= branch target PC[22:1])
0.U(1) // LSB=0: 2B sort
  )
else:
→ Use fallThrough prediction
```

**2-taken feature status (TODO)**:

```
Design intent:
- Simultaneously store/predict slot1 + slot2 and two taken branches in one fetch block
- slot1 = main taken branch (always-taken), slot2 = second taken branch candidate (including direction bit)
- Currently, uBTB processes only 1 taken branch (slot1) — throughput is expected to improve when 2-taken is implemented

Current status:
- Bundles.scala:50-55: slot2 field defined (valid, taken, position, attribute, target)
- MicroBtb.scala:193: Fixed slot2.valid := false.B during training (always disabled)
- MicroBtb.scala:78-83: Prediction logic does not reference slot2 at all — only slot1 is used
- Refer to slot2 in the entire codebase: only 2 places: declaration + false.B initialization

Conclusion: Only data structure is reserved, prediction/learning logic is not implemented (MicroBtb.scala class declaration // TODO: 2-taken)
```

**Pair direction predictor**: `uTAGE` (S1 simultaneous result, direction override to `s1_utageHitMask`)

---

### 11.3 ABTB (Ahead BTB) — S1 Stage

#### 11.3.0 Why ABTB exists

**Problem 1: Capacity limitations of uBTB**

uBTB is a 32 entries fully-associative structure. In large programs, there are many cold misses and coverage is insufficient.

**Problem 2: Fully-Associative is not scalable**

As the number of entries increases, the comparator increases linearly → timing and area costs rapidly increase. A Set-Associative structure is required.

**Problem 3: If you just use Set-Associative SRAM, S1 timing will not be correct**

By simply introducing Set-Associative BTB, PC_B is committed mid-cycle after uBTB output reconfiguration, causing SRAM access to start late:

```
[If you use a regular Set-Assoc BTB without the ahead trick]

Cycle N:   BPU S0 = PC_A.
Cycle N+1: PC_B confirmed with uBTB output → middle of cycle
Normal BTB: setIndex(PC_B) → start in the middle of SRAM read cycle
Cycle N+2: BPU S1 = PC_B.
SRAM response arrives mid-cycle → tag comparison late → S1 timing violation!
→ Results can only be used in S2 (1 cycle loss)

[ABTB’s ahead trick]

Cycle N: BPU S0 = PC_A.  PC_A is already a stable value.
ABTB S0: Start reading SRAM with setIndex(PC_A) → Early cycle, secure full cycle
Cycle N+1: Arrives early in the SRAM response cycle. s1_entries stable.
s1_startPc = io.startPc = PC_B (already stable)
tag Comparison cycle starts early, results latch at end of cycle
Cycle N+2: BPU S1 = PC_B.  Combination output from s2_* registers → S1 timing met!
```

The key to the ahead trick: **Advance SRAM access by 1 cycle**, allowing even large Set-Associative SRAMs to produce results within S1 timing. Without the trick, ABTB is relegated to the S2 predictor.

**Problem 4: uBTB does not support Multi-Branch**

uBTB stores only one slot1. Since ABTB tag = fetch block PC, multiple branches of one fetch block are simultaneously stored in 8 ways → Richer branch coverage.

**summation:**

| Reason | Description |
|---|---|
| capacity shortage | uBTB 32 entries → insufficient coverage |
| Scalability | Fully-Assoc expansion not possible → Set-Assoc required |
| Timing | Set-Assoc SRAM does not allow S1 timing → solved with ahead trick |
| Multi-Branch | Simultaneous cache of multiple branches of one fetch block in 8 ways |

---

| Item | value |
|---|---|
| BTB method | **Block BTB (multiple branches)** — tag = fetch block start PC[24:1], multiple ways share the same tag, returns up to 8 branches simultaneously |
| structure | **Set-Associative, Banked** |
| Total Entries | **1,024** |
| Banks | **4** (read-write conflict resolution) |
| Ways | **8** per set |
| Sets per bank | **32** (= 1024 / 8 / 4) |
| Tag Width | **24 bits** |
| Target Lower Bits | 22 bits (2B aligned) |
| Taken Counter | 2-bit |
| Write Buffer | 4 entries |
| Replacer | PLRU |
| Use History | **None** |
| Predicted Latency | **Depends on standard**: 2 cycles based on BPU s0 input PC / 1 cycle based on BPU s1 current block |

> Standard summary:
> - Based on BPU s0 input PC (PC_A): two-block-ahead character seen as `PC_A -> PC_B -> PC_C`
> - Based on BPU s1 current block (PC_B): `PC_B -> PC_C` prediction (non-lookahead)

**AddrField bit structure** (`abtb/Helpers.scala:24`):
```
PC bit : [ 0  ] [ 2:1 ] [ 7:3  ] [ VAddrBits-1:8 ]
field  :  inst   bank    setIdx      unused
          Off   (2 bit)  (5 bit)

extraField:
  tag         = PC[24:1]   (start=instOffsetBits=1, width=24)
  targetLower = PC[22:1]   (start=instOffsetBits=1, width=22)
```

| field | PC bits | Remarks |
|---|---|---|
| instOffset | PC[0:0] | 1 bit |
| bankIdx | **PC[2:1]** | 2 bits → 4 banks selection |
| setIdx | **PC[7:3]** | 5 bits → 32 sets/bank selection |
| tag | **PC[24:1]** | 24 bits (including bankIdx+setIdx bits) |

**[Core] ABTB's tag is fetch block PC** (`abtb/Bundles.scala:85`):
```
AheadBtbEntry.tag = PC[24:1] ← fetch block start PC, not individual branch PC
```
> General set-associative BTB tag = individual branch instruction PC → maximum 1 way hit per lookup.
> ABTB tag = fetch block start PC → **multiple branches within the same fetch block** are stored in each way.
> Therefore, if you look up the same fetch block, **multiple ways may hit** at the same time (normal operation).
> Ways are distinguished by different `position` (branch positions within the fetch block).

**Lookup method** (`abtb/AheadBtb.scala:107-178`):

> **Note**: ABTB uses different PCs for SRAM index and tag comparison (ahead mechanism).

**[Key] Why are set-index(PC_A) and tag(PC_B) different PCs?**

In general BTB, set-index and tag originate from the same PC. ABTB is different:

| role | Regular BTB | ABTB |
|------|----------|------|
| set-index | PC_B[7:3] → SRAM which row? | **PC_A[7:3]** → SRAM which row? (Read 1 cycle ahead) |
| tag | PC_B[24:1] → "Is this entry for PC_B?" | **PC_B[24:1]** → "Is this entry for PC_B?" |
| entry data | Branch information in PC_B | Branch information in PC_B |

- **set-index(PC_A)**: Pure physical address — "Which row of SRAM to read?" PC_A is used to consume SRAM 2-cycle latency 1 cycle ahead.
- **tag(PC_B)**: Logical identifier — "Which of the read entries is for the PC_B block?" For confirmation of affiliation.

**What if tag is used as PC_A?**
- `entry.tag === getTag(PC_A)` → "Does this entry record a branch of the PC_A block?" search for
- Such entries are **never saved** (because they are saved as `tag = getTag(PC_B)` during training)
- The branch of the PC_A block has already been processed much earlier in the cycle — the information needed now is the branch of the PC_B block.

**Meaning of ABTB entry**: "When executing the PC_A → PC_B path, which branch (position) in the PC_B block jumps to (target)?"
- `setIndex(PC_A)`: SRAM physical location (index for ahead access, stored at the time of moving to the next block)
- `tag = getTag(PC_B)`: Logical identifier indicating that this entry belongs to the PC_B block
- `data(position, target)`: Actual branch information in PC_B → Next target PC = PC_C

```
[S0] SRAM read (based on previous block PC_prev):
BankIndex = PC_prev[2:1] → Select the relevant bank (s0_previousStartPc = io.startPc at cycle N)
SetIndex = PC_prev[7:3] → select set (32 sets)
s0_previousStartPc = io.startPc ← Include “previous” in the variable name: “Previous” block of the next cycle

[S1] Wait for SRAM response + capture current BPU S0 PC:
s1_startPc = io.startPc ← Not registered (RegEnable)! LIVE value (=current BPU S0 PC=PC_curr)
s1_entries = SRAM response (based on setIndex(PC_prev))

[S2] Tag comparison (based on current block PC_curr):
s2_startPc = RegEnable(s1_startPc) = PC_curr ← A different PC than the PC used to read SRAM!
  s2_tag     = getTag(s2_startPc) = PC_curr[24:1]

// 8 ways simultaneous tag comparison → hitMask creation
  s2_hitMask[i] = s2_entries[i].valid && s2_entries[i].tag === s2_tag
// entries: value read from setIndex(PC_prev)
  //   tag:     PC_curr[24:1]
// → HIT if there is an entry previously saved as (setIndex(PC_prev), tag=PC_curr)

// hitMask can turn on multiple bits — multiple branches of the PC_curr block are stored in each way
// Example) If branch@pos3, branch@pos7, and branch@pos12 exist in the PC_curr block,
// way0(tag=PC_curr, pos=3), way1(tag=PC_curr, pos=7), way2(tag=PC_curr, pos=12) all hit

// Prediction output for each hit way (AheadBtb.scala:172-178)
  io.prediction[i].valid       := s2_valid && s2_hitMask[i]
  io.prediction[i].taken       := takenCounter[bankIdx][setIdx][i].isPositive
  io.prediction[i].cfiPosition := s2_entries[i].position
  io.prediction[i].target      := reconstruct(s2_startPc, s2_entries[i])
// → Simultaneous output of up to 8 Valid[Prediction] (valid=true as many ways as hit)
// → BPU receives this result from S1 in PC_curr (arrived in a timely manner thanks to ahead)
```

> **Exception handling** (`s2_multiHit`): Two ways with the same position hit simultaneously → This is anomaly.
> In the normal case, all hit ways have different positions (`predict_hit_entry_num` perf counter).

**Entry Contents** (`abtb/Bundles.scala:83`):
```
AheadBtbEntry { // 1 entry = 1 branch (one branch in the fetch block)
  valid:           Bool
tag: UInt(24) // fetch block start PC[24:1] (not individual branch PCs!)
position: UInt(5) // CfiPosition: Branch position within the fetch block (separator between ways)
  attribute:       BranchAttribute(4)    // branchType(2) + rasAction(2)
  targetLowerBits: UInt(22)              // partial target: branch target PC[22:1]
// targetCarry: Not included (EnableTargetFix=false)
}

// 8 ways × 32 sets × 4 banks = 1,024 entries total
// Multiple ways within the same set can have the same tag (= same fetch block PC)
// → Up to 8 branches can be cached for one fetch block
```

**[Key] Meaning of 8 ways — Multiple Branches in Fetch Block, not Branch Tree**

8 ways indicates “the maximum number of branch instructions that the next fetch block (PC_B) can have.”
This is **not** a multi-level branch tree — multiple branches within a single fetch block.

```
SRAM row read with setIndex(PC_A):

way0: {tag=PC_B, position=2, target=PC_C1} ← PC_B block offset 2nd instruction branch
way1: {tag=PC_B, position=5, target=PC_C2} ← PC_B block offset 5th instruction branch
way2: {tag=PC_B, position=11, target=PC_C3} ← PC_B block offset 11th instruction branch
way3: {tag=PC_K, position=7, target=PC_C4} ← Other path PC_A → branch of PC_K (different tag)
  ...

// Same tag (=PC_B) → Multiple branches within the same fetch block → Simultaneous hits are normal
// Other tag (=PC_K) → Entry in another path (PC_A → PC_K) → Hit only when PC_K fetch
```

Two roles in 8 ways:

| role | Description |
|------|------|
| **Same tag, different position** | Simultaneous cache of multiple branch instructions in PC_B block (maximum 8) |
| **Other tag** | Coexistence of entries from other paths (PC_A→PC_K, etc.) that share setIndex(PC_A) |

**To avoid misunderstanding**: It does not cache the branch tree as in "PC_A → PC_B (2 branches) → 8 final destinations".
ABTB covers the fact that **there may be multiple branches within a single next fetch block (PC_B)**.

**Next PC decision** (processed by Bpu.scala):
```
// Receive up to 8 Valid[Prediction] from ABTB (one for each branch instruction in PC_B block)
// 1 output of uBTB and concat → s1_btbPrediction[0..8] (up to 9 in total)
// CompareMatrix: Select minimum position among those taken → single s1_prediction

// Example) way0(pos=2, taken), way1(pos=5, not-taken), way2(pos=11, taken)
// → What is taken: min position among way0 and way2 = way0(pos=2)
//     → next PC = PC_C1
//
// Reason: Within the fetch block, the branch with the smallest position is executed first.
// → If the branch is taken, subsequent instructions will not be executed.
// → The target of “first taken branch” is next fetch PC
```

**Feature**: Cache up to 8 branches within a fetch block simultaneously and return them all in one lookup.
CompareMatrix selects the “first branch taken” to determine the next PC.

**Pair direction predictor**: `uTAGE` (shared with uBTB, S1 simultaneous results)

**Fast Training**: `BpuFastTrain` (s3_valid signal) → Fast training with S3 results

---

### [Core] ABTB’s “Ahead” pipeline mechanism

#### Compare the paths that two predictors take to reach the same BPU S1

Although uBTB and ABTB index different PCs in different cycles, they are output simultaneously on the same BPU S1 for PC_B.

```
Cycle N:   BPU S0 = PC_A
uBTB: Register comparison (combinational) at PC_A
ABTB: Start reading SRAM at setIndex(PC_A) ← Physical SRAM, takes 1 cycle

Cycle N+1: BPU S1 = PC_A (→ determine next s0_startPc = PC_B with uBTB output)
uBTB → PC_A Prediction output: target = PC_B ← 1-cycle delay from PC_A
uBTB: Start new register comparison with PC_B
ABTB: SRAM response arrives, tag comparison begins (s1_startPc = PC_B live)

Cycle N+2: BPU S1 = PC_B
uBTB → PC_B prediction output: target = PC_C ← 1-cycle delay from PC_B (N+1)
ABTB → PC_B prediction output: target = PC_C ← 2-cycle delay from PC_A (N)
↑ Both arrive simultaneously from BPU S1 for PC_B!
CompareMatrix → Select minimum cfiPosition → s1_prediction → next s0_startPc = PC_C
```

**Key points:**
- uBTB: **1-cycle** (register-based, starts from PC_B)
- ABTB: **2-cycle** (based on physical SRAM, starting from PC_A) — ahead trick pulls 1-cycle
- In the same ABTB, if the starting point is set to `PC_B` (BPU s1 current block), the result appears to be **1-cycle**.
- Both paths reach the same destination (BPU S1 for PC_B)

#### Why is ABTB so much larger that it can be printed in the same cycle?

| | uBTB | ABTB |
|---|---|---|
| Storage method | **Register** (CAM/FF) | **Physical SRAM** |
| given pipeline cycle | **1-cycle** | **2-cycle** (ahead trick) |
| Cycle N: | PC_A immediate comparison (combinational) | setIndex(PC_A) Read SRAM |
| Cycle N+1: | PC_B immediate comparison (combinational) | SRAM response + tag comparison (s1_startPc=PC_B) |
| Cycle N+2: | PC_B result output | PC_B result output |

Since uBTB is register-based, 1-cycle comparison is sufficient. ABTB is a physical SRAM, so it originally requires 2-cycle, but the **ahead trick advances SRAM access by 1-cycle** and matches the 2-cycle pipeline to the same BPU S1. Even if the SRAM size is much larger, the timing can be met because a 2-cycle budget is given.

#### entry storage method

```
Save entry:
setIndex = setIndex(PC_A) ← SRAM address to previous block PC
tag = getTag(PC_B) ← Current (next) block PC
data = branch information taken in PC_B (position, target=PC_C, attribute)
```

#### Detailed pipeline timeline

```
Cycle N:   BPU S0=PC_A. ABTB S0 fires.
→ Start reading SRAM: setIndex(PC_A)
Cycle N+1: BPU S0=PC_B, S1=PC_A.
             ABTB S1 fires (s1_fire = s1_valid && predictReqValid).
→ s1_startPc = io.startPc = PC_B ← LIVE value, not registered!
→ s1_entries = SRAM response from setIndex(PC_A)
Cycle N+2: BPU S0=PC_C, S1=PC_B.
             ABTB S2 fires (s2_fire = s2_valid && predictionSent=BPU.s1_fire).
             → s2_startPc = PC_B, s2_tag = getTag(PC_B)
             → hitMask: entries[setIndex(PC_A)] vs tag=getTag(PC_B)
→ io.prediction output
BPU S1 for PC_B: uBTB + ABTB simultaneous output → CompareMatrix → s1_prediction
→ s2_abtbMeta capture: setIdx=setIndex(PC_A), valid=true
Cycle N+3: BPU S2=PC_B. s3_abtbMeta = s2_abtbMeta, s3_startPc = PC_B.
Cycle N+4: BPU S3=PC_B. fastTrain fires:
             → fastTrain.startPc         = s3_startPc = PC_B
             → fastTrain.abtbMeta.setIdx = setIndex(PC_A)
→ fastTrain.finalPrediction = s3_prediction (final prediction of PC_B)
```

#### [Core] ABTB Training mechanism

**ABTB only uses fastTrain — no FTQ commit train path**

`AheadBtb.scala:204-206`:
```scala
private val t0_train = io.fastTrain.get.bits
private val t0_fire  = io.enable && io.fastTrain.get.valid
                       && t0_train.finalPrediction.taken
                       && t0_train.abtbMeta.valid
```

ABTB does not have a `io.train` (FTQ commit path) consumption code. Consumes only `io.fastTrain`. thus:
- Training point: **BPU S3** (~3 cycles after PC_B enters BPU S1)
- **Not “train when PC_C commits”** — Completed well before commit (~20-30 cycles later)

**Training Timing**:
```
Cycle N: BPU S0 = PC_A → Start reading ABTB SRAM (setIndex(PC_A))
Cycle N+1: BPU S1 = PC_B → abtbMeta capture: setIdx=setIndex(PC_A)
Cycle N+2 : BPU S2 = PC_B → s2_abtbMeta → s3_abtbMeta
Cycle N+3: BPU S3 = PC_B → fastTrain occurs ← Training HERE
Cycle N+4: t1_fire → takenCounter update + SRAM write (if necessary)

commit: Cycle N+20~30 ← ABTB does nothing at this time
```

**Training data source** (`Bpu.scala:182-186`, `AheadBtb.scala:272-275`):

| SRAM Field | value | Source |
|---------|-----|------|
| setIdx | `setIndex(PC_A)` | `abtbMeta.setIdx` (captured from BPU S1) |
| tag | `getTag(PC_B)` | `fastTrain.startPc = s3_startPc` |
| position | Location within the fetch block of the taken branch | `s3_prediction.cfiPosition` |
| target | PC_C | `s3_prediction.target` |

→ Result: entry at `setIndex(PC_A)`, tag=`getTag(PC_B)`, data=taken branch information of PC_B (target=PC_C)

**abtbMeta lifetime — 3 cycles, no FTQ storage**:
```
// Bpu.scala:151: "abtb meta won't be sent to ftq, used for abtb fast train"

BPU S1 fire → s2_abtbMeta (register)
BPU S2 fire → s3_abtbMeta (register)
BPU S3 → Consume with fastTrain → Overwrite with the next value
```

The meta of other predictors (MBTB, TAGE, etc.) is stored in the FTQ entry and preserved until commit, but abtbMeta exists only in the BPU internal register for 3 cycles and then disappears. This is why there is no abtbMeta field in the FTQ entry.

**Training content — two updates** (`AheadBtb.scala:234-266`):

1. **takenCounter (register, update immediately)**:
```scala
needDecrease = updateThisSet && isCond && (!t1_trainTaken || (t1_trainTaken && posBefore))
// conditional branch whose position is ahead of the taken branch → was actually not taken → decreases

needIncrease = updateThisSet && isCond && t1_trainTaken && posEqual
// conditional branch whose position matches → actual taken → increment
```

2. **SRAM entry write (2 cases)**:
```
t1_needWriteNewEntry: The branch is not in ABTB → New allocation to victim way
t1_needCorrectTarget: Hit, but target lower bits of indirect branch mismatch → target modified
```

**Behavior in case of Misprediction**:
```
If you trained based on S3 prediction and it is actually wrong (redirect when commit):
→ BPU starts re-fetching from the correct PC
→ Relearn (overwrite) ABTB with correct info when the new path passes through BPU S3
ABTB learns quickly with the “best guess based on BPU S3”, and if it is wrong, it is corrected later.
```

**Prediction hit condition** (when revisiting the same path PC_A → PC_B):
```
ABTB S0: SRAM read at setIndex(PC_A)
ABTB S1: s1_startPc = io.startPc = PC_B  ← LIVE
ABTB S2: tag comparison: entry.tag(=getTag(PC_B)) === getTag(PC_B) → HIT!
→ Return branch information taken within PC_B → Pass to BPU S1 for PC_B
```

**Difference from “two-block ahead” in the paper:**
| | thesis two-block ahead | XiangShan ABTB |
|---|---|---|
| By index | PC_A (current block) | PC_A (previous block) |
| By tag | PC_C (two blocks ahead) | PC_B (one block ahead) |
| Prediction target | branch of PC_C | branch of PC_B |
| Purpose | double I-fetch or pipelined | SRAM latency hiding → maintaining S1 timing |

---

### 11.4 MBTB (Main BTB) — S2 Stage

| Item | value |
|---|---|
| BTB method | **Block BTB (multiple branches)** — tag = 32B-aligned half-block PC[31:16], multiple ways share the same tag, 2 AlignBanks × 4 ways = returns up to 8 branches |
| structure | **Set-Associative + 2-Level Banking** |
| Total Entries | **8,192** |
| Ways | **4** |
| Align Banks | **2** (64B fetch block → split into 2 32B half-blocks) |
| Internal Banks | **4** (SRAM power saving / read-write conflict distribution) |
| Sets per SRAM | **256** (= 8192 / 4 ways / 4 ibanks / 2 abanks) |
| Tag Width | **16 bits** |
| Target Width | 20 bits (2B aligned) |
| Taken Counter | **2-bit bimodal** (base predictor) |
| Write Buffer | 4 entries |
| Replacer | **LRU** |
| Use History | **None** |
| Predicted Latency | **2 cycle** |

**AddrField bit structure** (`mbtb/Helpers.scala:30`, `mbtb/Parameters.scala`):
```
PC bit : [ 4:0  ] [ 5 ] [ 7:6 ] [  15:8  ] [  31:16  ] [ VAddrBits-1:32 ]
field  :  align   aBank  iBank    setIdx       tag           unused
          Offset  (1b)   (2b)    (8 bits)    (16 bits)

extraField:
replacerSetIdx = PC[13:6] (FetchBlockSizeWidth=6 from 8 bits)
targetLower = PC[20:1] (instOffsetBits=1 to 20 bits)
position = PC[5:1] (instOffsetBits=1 to 5 bits)
cfiPosition = PC[6:1] (6 bits from instOffsetBits=1)
```

| field | PC bits | Remarks |
|---|---|---|
| alignOffset | PC[4:0] | 5 bits, 32B offset in alignment block |
| alignBankIdx | **PC[5:5]** | 1 bit → 2 align banks selection |
| internalBankIdx | **PC[7:6]** | 2 bits → 4 internal banks selection |
| setIdx | **PC[15:8]** | 8 bits → 256 sets/SRAM selection |
| tag | **PC[31:16]** | 16 bits → way selection (4-way comparison) |

**AlignBank vs InternalBank — 2-Level Banking Tier**:

MBTB uses banking for two independent overlapping purposes.

```
MBTB (total 8192 entries)
├── AlignBank[0] ← Responsible for the lower 32B section of the 64B fetch block
│   ├── InternalBank[0]   ← PC[7:6]=00 entries (physical SRAM)
│   │   ├── entrySRAM × 4 ways
│   │   └── counterSRAM
│   ├── InternalBank[1]   ← PC[7:6]=01
│   ├── InternalBank[2]   ← PC[7:6]=10
│   └── InternalBank[3]   ← PC[7:6]=11
└── AlignBank[1] ← Responsible for the upper 32B section of the 64B fetch block
    └── InternalBank[0..3]
Total SRAM: 2 × 4 × (4 entrySRAMs + 1 counterSRAM) = 40
```

**AlignBank (2) — Covers multiple branches of 64B fetch blocks** (`mbtb/Parameters.scala`):
```
NumAlignBanks = FetchBlockSize / FetchBlockAlignSize = 64B / 32B = 2
Parameter comment: "Highest level banks, alignment restriction"
```
- **Purpose**: To cover multiple branches spanning a 64B fetch block, parallel lookup by dividing the fetch block into two 32B half-blocks
- MBTB tag unit = 32B half-block (divided by PC[31:16] + alignBankIdx)
- Parallel lookup from the front 32B of one fetch block → AlignBank[A], and the back 32B → AlignBank[B]
- 4 ways in each AlignBank → returns a total of 8 branch predictions
- **VecRotate operation** (`MainBtb.scala:76-84`):
  ```scala
s0_startPcVec(0) = s0_startPc // 32B half-block to which startPc currently belongs
s0_startPcVec(1) = aligned(s0_startPc + 32B) // then 32B half-block
  → rotate by getAlignBankIndex(s0_startPc)
// Result: AlignBank[i] always receives a PC matching its alignIdx
  ```
- **startPc offset filtering** (`MainBtbAlignBank.scala:166`):
  ```scala
  val hit = rawHit && e.position >= s2_alignedInstOffset && !s2_crossPage
// If startPc starts in the middle of a 32B half-block, branch entries before startPc are excluded.
// (AlignBank[1] = always aligned PC → alignedInstOffset=0 → no filter)
  ```
- **Coverage constraints**: Parameter comment: *"can provide at most (banks-1)/banks × predict width"*
- If startPc starts in the middle of a 32B half-block, the 64B fetch may spill out into the third 32B half-block.
- 2 AlignBanks do not cover the third part → worst-case 50% coverage is guaranteed

**InternalBank (4) — SRAM Power Savings + Read-Write Conflict Distribution** (`mbtb/Parameters.scala`):
```
NumInternalBanks = 4
Parameter comment: "Lowest level banks, read-write conflicts and reduce SRAM power"
```
- **Purpose**: Divide physical SRAM into 4 → Activate only 1 InternalBank in each cycle → Save 75% of power
- **Selection method** (`MainBtbAlignBank.scala`):
  ```scala
  s0_internalBankMask = UIntToOH(getInternalBankIndex(s0_startPc))
// getInternalBankIndex = PC[7:6] (2 bits) → Only 1 of InternalBank 0~3 is active
  ```
- **Conflict distribution**: If the PC[7:6] of the lookup PC and the update PC are different, they are different InternalBank → No conflict
- **Conflict processing**: Forwarding to WriteBuffer (4 entries) when hitting the same InternalBank
- **Physical SRAM Structure** (`MainBtbInternalBank.scala`):
  ```scala
entrySrams = Seq.tabulate(NumWay)(wayIdx => SRAMTemplate(...)) // Separate entry SRAM for each way
counterSram = SRAMTemplate(TakenCounter, set=NumSets, way=NumWay) // SRAM dedicated to counter
  ```

| | AlignBank | InternalBank |
|---|---|---|
| **Number** | 2 | 4 (per AlignBank) |
| **Purpose** | Cross-alignment processing | SRAM power saving + conflict distribution |
| **Activate** | All cycles are parallel active | Only 1 active per cycle (based on PC[7:6]) |
| **Holds SRAM** | None (routing layer) | Yes (entrySRAM × 4 + counterSRAM) |

**Lookup method** (`mbtb/MainBtbAlignBank.scala`):
```
// Actions per AlignBank (2 in parallel):
alignBankIdx = PC[5] → Select AlignBank (routing has already been completed in MainBtb top)
internalBankIdx = PC[7:6] → Select 1 of 4 internal banks (SRAM enabled)
SetIndex = PC[15:8] → Select from 256 sets
Tag = PC[31:16] → Compare tags simultaneously with all 4 ways

// Same as ABTB: multiple branches of the same 32B half-block are stored in each way
// → Multiple ways can match tags simultaneously (multi-hit normal operation)
rawHit[i] = entry[i].valid && entry[i].tag == s2_tag
hit[i]    = rawHit[i] && entry[i].position >= alignedInstOffset && !crossPage

// position condition:
// AlignBank[0] (including startPc): alignedInstOffset > 0 possible → exclude branch before startPc
// AlignBank[1] (next half-block): alignedInstOffset = 0 → fully inclusive

// Result: Up to 4 Valid[Prediction] outputs from each AlignBank
// → 2 AlignBanks flatten → total maximum of 8 (same structure as ABTB)
io.result = alignBanks.flatMap(_.io.read.resp.predictions) // Total 8
```

> PC[VAddrBits-1:32] is not included in the tag → Aliasing of the upper bits of the physical address is possible.
> Practically guarantees sufficient distinction within the 32-bit PC range (PC[31:0]).

**Entry Contents** (`mbtb/Bundles.scala:35`):
```
// MBTB uses two separate SRAMs (entry SRAM + counter SRAM)

// [1] Entry SRAM (structure information):
MainBtbEntry {
  valid:           Bool
tag: UInt(16) // PC[31:16], for comparing way selection
  attribute:       BranchAttribute(4)     // branchType(2) + rasAction(2)
position: UInt(4) // CfiAlignedPosition: branch position in alignBank
                                         // = CfiPosition(5) - alignBankIdx(1) → 4 bits
targetCarry: TargetCarry(2) // Fit/Overflow/Underflow (always save)
  targetLowerBits: UInt(20)             // partial target: branch target PC[20:1]
}

// [2] Counter SRAM (direction information, base predictor):
counters: Vec[4, SatCtr(2-bit)] // 4 ways each taken/not-taken bimodal counter
// → Use as TAGE base predictor
// TAGE/SC overrides this to determine the final direction
```

**Next PC Decision** (`mbtb/MainBtb.scala`, `Bpu.scala`):
```
// Receive 8 predictions from S2 (2 AlignBanks × 4 ways, same structure as ABTB)
io.result = alignBanks.flatMap(_.io.read.resp.predictions)  // Vec[8, Valid[Prediction]]

// Each prediction entry:
pred[i].valid       = rawHit && position >= alignedInstOffset && !crossPage
pred[i].cfiPosition = Cat(posHigherBits, entry.position)    // {alignBankIdx, 4-bit} = 5-bit
pred[i].target = reconstruct fullTarget (correct high bits with targetCarry)
pred[i].taken       = counter.isPositive  // bimodal base predictor

// Same as ABTB's multi-hit: multiple branches of the same 32B half-block are stored in each way
// → Multiple ways can hit at the same time (same tag, different position)
// → Valid ones out of 8 are sent to s2_mbtbResult of BPU top

// Determine direction (Bpu.scala s2_condTakenMask, applied independently for each entry):
// 1st priority: SC exceeds threshold → SC direction
// 2nd priority: TAGE provider table hit → TAGE direction
// 3rd place: pred[i].taken (MBTB bimodal counter)
finalTaken[i] = Mux(sc.scUsed[i],       sc.scTakenMask[i],
                Mux(tage.useProvider[i], tage.providerPred[i],
                                         pred[i].taken))

// Final branch selection: CompareMatrix → taken entry of minimum cfiPosition → single s3_prediction
```

**Pair direction predictor**: `TAGE` (8-table) + `SC` (Statistical Corrector)
- MBTB’s built-in 2-bit counter = **TAGE base predictor** (fallback)
- TAGE full predictor overrides provider table

---

### 11.5 History Types — PHR vs GHR

> **Key**: In this implementation, TAGE / uTAGE / ITTAGE / SC(path) all use **PHR(Path History Register)**.
> It is different from the GHR (branch direction taken/not-taken record) of traditional TAGE.

#### 11.5.1 PHR (Path History Register) — `bpu/history/phr/`

Every quarter, `(PC, target)` pairs are summarized and accumulated as a 15-bit hash.

```
pathHash(pc, target) = Cat(PC[9:1], 4'b0000)  XOR  target[16:2]
                       ←─── 9 bits of PC ───→      ←─ 15 bits ─→
Result: 15 bits (PathHashWidth = 15)
```

PHR update method: Shift Shamt=2 bits to the left every quarter and then XOR the new hash
```
PHR_new = (PHR_old << 2) XOR pathHash(pc, target)
```
- PHR maximum cumulative length: **397 bits** (based on histLen in TAGE Table 7)
- `FoldedHistory`: XOR-fold the long bit string of PHR into N-bits and supply it to the predictor

**XOR-Fold Algorithm** (`computeFoldedHist`, `bpu/Helpers.scala:172`):
```
FoldPHR(N) = Divide PHR into N-bit chunks and XOR them all
Example) PHR[397:0] → fold to 9 bits:
    chunk0 = PHR[8:0]
    chunk1 = PHR[17:9]
    ...
    chunk44 = PHR[396:387]  (9 bits, zero-padded)
    FoldPHR(9) = chunk0 XOR chunk1 XOR ... XOR chunk44
```

#### 11.5.2 GHR (Global History Register) — `bpu/history/commonhr/`

Record taken/not-taken results of conditional branch as bit stream. Updated upon S3 fire.

- To be used only in **SC global table (GlobalEnable)**, but currently **`GlobalEnable = false`** (inactive)
- **SC reverse table (BWEnable)** is also **`BWEnable = false`** (inactive)

#### 11.5.3 PC bit extraction principle (AddrField)

`AddrField` allocates fields sequentially starting from bit 0 (`utils/AddrField.scala`):
```
extract(fieldName, pc) = pc[end:start] (start/end are sequential accumulation)
```

**Common Prerequisites** (XiangShan KMH v3, RVC enabled):
- `instOffsetBits = 1` (2B alignment, bit 0 = always 0)
- `FetchBlockAlignWidth = 5` (32B = FetchBlockSize/2)
-`VAddrBits = 50` (PrunedAddr length)

---

### 11.6 uTAGE (Micro TAGE) — Index/Tag Hashing details

Pair with uBTB/ABTB. S1 1-cycle results. **`MicroTageTable.scala:83`**

```
unhashedIdx = PC[VAddrBits-1 : 1]   (PC >> instOffsetBits)
unhashedTag = PC[VAddrBits-1 : 7]   (PC >> PCHighTagStart=7)
```

#### Table 0 (512 sets, histLen=9, histBitsInTag=9, tagLen=15)

| Item | FoldedLength | formula |
|---|---|---|
| idxFhInfo | min(9, 9) = **9 bits** | PHR[9-1:0] fold → 9 bits |
| tagFhInfo | min(9, 9) = **9 bits** | PHR fold → 9 bits |
| altTagFhInfo | min(9, 8) = **8 bits** | PHR fold → 8 bits |

```
SetIndex = (PC[9:1] XOR FoldPHR_9)[8:0]

lowTag   = (PC[VAddrBits-1:7] XOR FoldPHR_9 XOR (FoldPHR_8 << 1))[8:0]
highTag  = Cat(PC[16], PC[14], PC[12], PC[10], PC[8], PC[7], PC[6],  ← 11 bits
PC[5], PC[4], PC[3], PC[2]) (non-consecutive selection)
Tag      = Cat(highTag, lowTag)[14:0]                               ← 15 bits 截断
         = {highTag[5:0], lowTag[8:0]}
```

> highTag applies the `PCTagHashBitsForShortHistory = [15,13,11,9,7,6,5,4,3,2,1]` index to `unhashedIdx`.
> Since `unhashedIdx[i] = PC[i+1]`, the actual PC bit is each index + 1.

#### Table 1 (512 sets, histLen=16, histBitsInTag=12, tagLen=16)

| Item | FoldedLength | formula |
|---|---|---|
| idxFhInfo | min(16, 9) = **9 bits** | PHR fold → 9 bits |
| tagFhInfo | min(16, 12) = **12 bits** | PHR fold → 12 bits |
| altTagFhInfo | min(16, 11) = **11 bits** | PHR fold → 11 bits |

```
SetIndex = (PC[9:1] XOR FoldPHR_9)[8:0]

lowTag   = (PC[VAddrBits-1:7] XOR FoldPHR_12 XOR (FoldPHR_11 << 1))[11:0]
highTag  = Cat(PC[19], PC[17], PC[15], PC[13], PC[11],  ← 10 bits
               PC[7],  PC[6],  PC[5],  PC[3],  PC[2])   (PCTagHashBitsForMediumHistory)
Tag      = Cat(highTag, lowTag)[15:0]                   ← 16 bits 截断
         = {highTag[3:0], lowTag[11:0]}
```

- Counter: 3-bit signed, Useful: 2 bits
- **Fast Training**: Learning with S3 results (`BpuFastTrain`). No commit-based learning (no consecutive prediction blocks)

---

### 11.7 TAGE (Main TAGE) — Index/Tag Hashing details

Pair with MBTB. S2 Results. **`tage/Helpers.scala:51`, `tage/TageTable.scala`**

**AddrField structure** (same structure for 8 tables, 4096 total / 4 banks / 2 ways = **512 sets**):
```
PC bit  : [ 0 ] [ 2:1 ] [ 11:3 ] [ 24:12 ] [ 49:25 ]
field   :  inst   bank   setIdx     tag      unused
          Offset  Idx    (9 bit)  (13 bit)
```

| field | PC bits | Remarks |
|---|---|---|
| instOffset | PC[0:0] | 1 bit, always 0 (RVC 2B alignment) |
| bankIdx | PC[2:1] | 2 bits, 4 banks |
| setIdx | PC[11:3] | 9 bits, 512 sets/bank |
| tag | PC[24:12] | 13 bits |

```
// Create 3 FoldedHistory for each of 8 tables
idxFhInfo    : FoldedLength = min(histLen, 9)   → FoldPHR_idx
tagFhInfo    : FoldedLength = min(histLen, 13)  → FoldPHR_tag
altTagFhInfo : FoldedLength = min(histLen, 12)  → FoldPHR_altTag

// tage/Helpers.scala:34
forIdx = FoldPHR_idx
forTag = FoldPHR_tag XOR Cat(FoldPHR_altTag, 0.U(1.W))
       = FoldPHR_tag XOR (FoldPHR_altTag << 1)   ← 13 bits

// tage/Helpers.scala:64-65
BankIndex = PC[2:1] ← SRAM bank selection
SetIndex  = PC[11:3] XOR forIdx                   ← 9 bits

// tage/Helpers.scala:67-68
RawTag    = PC[24:12] XOR forTag                  ← 13 bits

// Tage.scala (CfiPosition XOR when tag matching)
FinalTag = RawTag
```

**Example of FoldPHR bit range by histLen:**

| Table | histLen | FoldPHR_idx (9 bit) | FoldPHR_tag (13 bit) |
|---|---|---|---|
| 0 | 4 | PHR[3:0] → fold4→pad9 | PHR[3:0] → fold4→pad13 |
| 3 | 29 | PHR[28:0] → fold9 | PHR[28:0] → fold13 |
| 7 | 397 | PHR[396:0] → fold9 | PHR[396:0] → fold13 |

> `fold4→pad9`: FoldedLength=4 < When SetIdxWidth=9, unlike uTAGE, TAGE takes 9-bit after simple XOR.
> If histLen is short, only the lower histLen bits are valid in the folded result, and the remaining padding = 0.

---

### 11.8 TAGE Base vs TAGE Full

| Category | TAGE Base | TAGE Full |
|---|---|---|
| Implementation | Built-in MBTB **2-bit bimodal counter** | 8 tagged history table |
| role | **default prediction** when no table is hit | Sophisticated predictions with Provider/Alternate logic |
| Use History | **None** | PHR up to **397 bits** route history |
| selection logic | simple taken/not-taken | Provider (longest matching history) → Alternate if weak |
| Override Relationship | Base = fallback | Full overrides base |

**Provider selection logic**:
```
if (provider table hit && !(useAltOnNA && counter is weak)):
    prediction = provider.isTaken
else if (alternate table hit):
    prediction = alternate.isTaken
else:
    prediction = MBTB bimodal (base)
```

---

### 11.9 SC (Statistical Corrector) — Implementation Details

A predictor that statistically detects and corrects when TAGE is biased in a specific direction. **`sc/Sc.scala`, `sc/Helpers.scala`**

> **Loop predictor ("L" in TAGE-SC-L)**: **Not implemented** in this codebase. There is only `loop_enable` field in `BPUCtrl` and no actual module.

---

#### 11.9.1 Table structure

| Table | active | Sets | histLen | By WayIdx | SetIndex formula |
|---|---|---|---|---|---|
| PathTable[0] | **True** | 128 | 8 | cfiPos[2:0] | `(PC[11:5] XOR FoldPHR_7)[6:0]` |
| PathTable[1] | **True** | 128 | 16 | cfiPos[2:0] | `(PC[11:5] XOR FoldPHR_7)[6:0]` |
| GlobalTable[0] | False | 128 | 8 | — | `(PC[11:5] XOR FoldGHR_7)[6:0]` ← GlobalEnable=false |
| GlobalTable[1] | False | 128 | 16 | — | `(PC[11:5] XOR FoldGHR_7)[6:0]` ← Inactive |
| BWTable[0] | False | 128 | 4 | — | `(PC[11:5] XOR FoldBW_7)[6:0]` ← BWEnable=false |
| BWTable[1] | False | 128 | 8 | — | `(PC[11:5] XOR FoldBW_7)[6:0]` ← Inactive |
| BiasTable | **True** | 128 | — | `Cat(cfiPos[2:0], {tageweak,tagetaken})` | `PC[11:5][6:0]` |

```
Common Structure:
NumWays = NumBtbResultEntries = 8 ← Number of MBTB result entries
WayIdx = cfiPosition[2:0] ← Branch position lower 3 bits within fetch block
BankIndex = PC[4:4] ← PC >> (1+3) lower 1 bit
  Counter   = 6-bit signed saturating    ← ScEntry.ctr

  BiasTable WayIdx = Cat(cfiPos[2:0], tageweak(1), tagetaken(1))  ← 5 bits → 32 ways
    tageweak  = provider.valid && provider.ctr.isWeak
    tagetaken = provider.valid && provider.ctr.isPositive
→ Save “Bias when TAGE is weakly taken” in a separate cell
```

---

#### 11.9.2 Core formula — percsum

```scala
// Helpers.scala:61
def getPercsum(ctr: SInt): SInt = Cat(ctr, 1.U(1.W)).asSInt
// Meaning: percsum = 2 * ctr + 1 (7-bit signed)
```

| ctr (6-bit signed) | percsum |
|---|---|
| +31 (maximum saturation) | **+63** (strong taken) |
| +1 (weak taken) | **+3** |
| 0 (neutral) | **+1** (≠0: dead-zone removal) |
| -1 (weak not-taken) | **-1** |
| -32 (maximum saturation) | **-63** (strong not-taken) |

> Reason for adding LSB=1: In the neutral state of ctr=0, percsum=+1 eliminates the dead-zone that becomes 0 when summed.

---

#### 11.9.3 Prediction Pipeline (S1→S2)

```
S1 (SRAM read complete):
s1_sumPercsum[wayIdx] = Σ percsum(pathTable[k][wayIdx].ctr) (k=0,1 active only)

S2 (MBTB results arrive):
  biasWayIdx[i] = Cat(wayIdx, {tageweak, tagetaken})
  totalPercsum[i] = s1_sumPercsum[wayIdx] + biasPercsum[biasWayIdx]

// Determine effective threshold according to TAGE provider confidence
threshold T = scThreshold[wayIdx].value >> 3 (initial 720 >> 3 = 90)

TAGE Saturate(isSaturate): effectiveThres = T >> 1 = 45
TAGE middle(isMid): effectiveThres = T >> 2 = 22
TAGE weak (isWeak): effectiveThres = T >> 3 = 11

  scUsed[i] = hit && tagePredValid && aboveThreshold(totalPercsum[i], effectiveThres)
scPred[i] = (totalPercsum[i] >= 0) ← SC prediction direction

Intuition: The more confident TAGE is, the stronger the signal is needed for SC to overturn.
```

```scala
// Helpers.scala:63
def aboveThreshold(scSum: SInt, threshold: UInt): Bool =
  (scSum > threshold.zext) && pos(scSum) ||
  (scSum < -threshold.zext) && neg(scSum)
```

---

#### 11.9.4 Training

```
Table ctr update condition:
  needUpdate = writeValid
             && tagePredValid
             && (scPred ≠ actual  ||  !sumAboveThres)
→ If the SC is wrong or the sum is less than the threshold, learn (if it exceeds the threshold and the correct answer is already enough → skip the learning)
Update direction: ctr.getUpdate(actual_taken)

Adaptive Threshold Updates:
  shouldUpdate = (tagePred ≠ scPred) && (scWrong || !sumAboveThres)
  threshold.getUpdate(scWrong)
→ If SC is wrong, threshold++ (use only when more certain)
→ If SC is correct, threshold-- (use more actively)
← Independent 12-bit saturating counter for each way
```

---

#### 11.9.5 Operation example

**Scenario**: Loop branch `0x1080` is taken 3 out of 4 times, not taken once. TAGE always predicts TAKEN.

```
branch: cfiPosition=5,  wayIdx = 5[2:0] = 5

[S2 prediction time]
TAGE provider: ctr=+1 (positive number, weak) → tageweak=1, tagetaken=1
biasWayIdx = Cat(5, 0b11) = 0b10_1011 = 43

PathTable[histLen=8 ]: ctr = -10 → percsum = -19
PathTable[histLen=16]: ctr = -15 → percsum = -29
  sumPercsum[5] = -48

BiasTable[43]:  ctr = -8  → biasPercsum = -15

totalPercsum = -48 + (-15) = -63

TAGE weak → effectiveThreshold = 90 >> 3 = 11
|−63| = 63 > 11 → scUsed = true
scPred = (−63 >= 0) = NOT_TAKEN   ← TAGE(TAKEN) override!
→ Final prediction: NOT_TAKEN ← Correct answer

[Learning point: actual = NOT_TAKEN]
  needUpdate = true && true && (NOT_TAKEN ≠ NOT_TAKEN || !sumAboveThres)
             = true && (false || !(63 > 90)) = true && true = true
→ ctr update: increases in NOT_TAKEN direction

  shouldUpdateThres = (tagePred=TAKEN ≠ scPred=NOT_TAKEN) && (scWrong=false || ...)
                    = true && (false || !(63>90)) = true && true = true
  threshold.getUpdate(scWrong=false) → threshold--
→ Use SC more easily next time
```

---

#### 11.9.6 Current implementation status

| component | status |
|---|---|
| PathTable (histLen=8,16) | **Active** (PHR based) |
| GlobalTable (histLen=8,16) | **Disabled** (GlobalEnable=false, GHR inactive) |
| BWTable (histLen=4,8) | **Disabled** (BWEnable=false) |
| BiasTable | **Active** (PC + TAGE Result Index) |
| Adaptive Threshold | **Active** (independent 12-bit counter for each way, initial value 720) |
| Loop predictor ("L") | **Not implemented** (Only BPUCtrl.loop_enable field exists) |

---

### 11.10 ITTAGE (Indirect Target TAGE) — Index/Tag Hashing details

Indirect branch target prediction. **`ittage/IttageTable.scala:94`**

```
unhashedIdx = PC >> instOffsetBits = PC[VAddrBits-1 : 1]
NumBanks = 2, bankIdxWidth = 1
```

| Table | nRows | histLen | setsPerBank | setIdxWidth | bankIdx | setIdxBase | setIdx | tagBase |
|---|---|---|---|---|---|---|---|---|
| 0 | 256 | 4 | 128 | 7 | PC[1] | PC[8:2] | `(PC[8:2] XOR FoldPHR_4)[6:0]` | PC[VAddrBits-1:9] |
| 1 | 256 | 8 | 128 | 7 | PC[1] | PC[8:2] | `(PC[8:2] XOR FoldPHR_7)[6:0]` | PC[VAddrBits-1:9] |
| 2 | 512 | 13 | 256 | 8 | PC[1] | PC[9:2] | `(PC[9:2] XOR FoldPHR_8)[7:0]` | PC[VAddrBits-1:10] |
| 3 | 512 | 16 | 256 | 8 | PC[1] | PC[9:2] | `(PC[9:2] XOR FoldPHR_8)[7:0]` | PC[VAddrBits-1:10] |
| 4 | 512 | 32 | 256 | 8 | PC[1] | PC[9:2] | `(PC[9:2] XOR FoldPHR_8)[7:0]` | PC[VAddrBits-1:10] |

**Tag formula** (same structure for all tables):
```
tagFhInfo    : FoldedLength = min(histLen, 9)   → FoldPHR_tag
altTagFhInfo : FoldedLength = min(histLen, 8)   → FoldPHR_altTag

Tag = (tagBase XOR FoldPHR_tag XOR (FoldPHR_altTag << 1))[8:0]   ← 9 bits

Example) Table 4 (histLen=32):
    Tag = (PC[VAddrBits-1:10] XOR FoldPHR_9 XOR (FoldPHR_8 << 1))[8:0]
```

- Region-based target compression: 16 regions, 2 ports, PLRU replacer
- TargetOffset: 20 bits (offset within region)

---

### 11.11 PC Bit usage comparison table (full summary)

| Predictor | history type | PC bits for index | PC bits for tag |
|---|---|---|---|
| **uBTB** | None | — (fully assoc) | Direct comparison of entire PC tags (22 bits) |
| **ABTB** | None | PC[7:3] (5 bits) | PC[24:1] (24 bits) |
| **MBTB** | None | PC[15:8] (8 bits) | PC[31:16] (16 bits) |
| **uTAGE T0** | PHR (path) | PC[9:1] XOR FoldPHR_9 | lowTag: PC[VAddrBits-1:7] XOR PHR; highTag: PC select bit concat |
| **uTAGE T1** | PHR (path) | PC[9:1] XOR FoldPHR_9 | lowTag: PC[VAddrBits-1:7] XOR PHR; highTag: PC select bit concat |
| **TAGE (all)** | PHR (path) | PC[11:3] XOR FoldPHR_idx | PC[24:12] XOR forTag XOR CfiPos |
| **SC path** | PHR (path) | PC[11:5] XOR FoldPHR_7 | None (tagless) |
| **ITTAGE T0~1** | PHR (path) | PC[8:2] XOR FoldPHR | PC[VAddrBits-1:9] XOR FoldPHR |
| **ITTAGE T2~4** | PHR (path) | PC[9:2] XOR FoldPHR | PC[VAddrBits-1:10] XOR FoldPHR |

---

### 11.12 RAS (Return Address Stack) — S3 Stage

| Category | uRAS (Micro RAS) | RAS (Full) |
|---|---|---|
| Stage | **S1** (fast speculative) | **S3** (commit-based) |
| Stack Size | — | Commit stack **16** + Speculative queue **32** |
| Stack Counter | — | 3-bit (merged call expression, maximum 7) |
| Use | ret target fast delivery | Restore correct ret address |

---

### 11.13 Maximum length by history

| History Type | maximum length | Usage Predictor |
|---|---|---|
| PHR (Path: PC+target hash) | **397 bits** (TAGE Table 7) | TAGE, uTAGE, ITTAGE, SC(path) |
| GHR (Global: taken/not-taken) | 16 bits | SC(global) ← Currently inactive |
| Backward History | 8 bits | SC(backward) ← Currently inactive |
| PHR per-branch hash window | **15 bits** | pathHash = Cat(PC[9:1],0000) XOR target[16:2] |

---

### 11.14 FastTrain mechanism

#### 11.14.1 Why do you need FastTrain separately?

The general `train` signal arrives after a delay of **20 to 30 cycles** through the path Backend → ROB commit → FTQ → BPU. Since the S1 predictors (uBTB, ABTB, uTAGE) are responsible for the fastest prediction path of the BPU, FastTrain is a **BPU internal S3 result that is immediately converted into a learning signal** to update them without delay.

```
General Train: BPU → FTQ → ROB execution → FTQ → BPU (~20~30 cycles)
FastTrain:   BPU S3 → BPU S1 predictor              (~2~3 cycles)
```

For `uTAGE`, FastTrain is set to **Required** because “there are no consecutive prediction blocks at the time of resolution” (`EnableFastTrain = true`, `utage/Parameters.scala`).

#### 11.14.2 BpuFastTrain bundle structure (`bpu/Bundles.scala`)

Comment: `// use s3 prediction to train s1 predictors`

```
BpuFastTrain {
startPc: PrunedAddr ← S3 block PC (by ABTB analysis: PC_B)
finalPrediction: Prediction { ← S3 final prediction
    taken:       Bool
    cfiPosition: UInt(5)
    target:      PrunedAddr
    attribute:   BranchAttribute
  }
hasOverride: Bool ← Has S3 overridden S1 prediction?
abtbMeta: AheadBtbMeta { ← ABTB S2 output meta (FTQ non-delivery, fastTrain only)
    valid:    Bool
setIdx: UInt ← setIndex(PC_A): SRAM read to previous block (ahead)
    bankMask: UInt
    entries:  Vec[8, {
hit: Bool ← Whether the corresponding way hit in S2
      attribute:       BranchAttribute
      position:        UInt(5)
      targetLowerBits: UInt(22)
    }]
  }
utageMeta: MicroTageMeta { ← uTAGE S1 output meta
histTableHitMap: Vec[2, Bool] ← Whether each uTAGE table hit or not
histTableTakenMap: Vec[2, Bool] ← Taken prediction of each table
histTableUsefulVec: Vec[2, UInt] ← useful counter
histTableCfiPositionVec: Vec[2, UInt] ← CFI position of each table
baseTaken: Bool ← ABTB based prediction direction
    baseCfiPosition:         UInt
  }
}
```

#### 11.14.3 Create and Deliver (`Bpu.scala:182-195`)

```scala
fastTrain.valid                := s3_valid
fastTrain.bits.startPc         := s3_startPc          // PC_B
fastTrain.bits.finalPrediction := s3_prediction // S3 final prediction
fastTrain.bits.abtbMeta := s3_abtbMeta // ABTB meta captured during BPU S1(PC_B) fire
fastTrain.bits.utageMeta       := s3_utageMeta
fastTrain.bits.hasOverride     := s3_override

predictors.foreach { p =>
p.io.fastTrain.foreach(_ := fastTrain) // Passed only to predictors inherited from HasFastTrainIO
}
```

**abtbMeta capture path** (`Bpu.scala:151-153`):
```scala
// abtb meta won't be sent to ftq, used for abtb fast train
private val s2_abtbMeta = RegEnable(abtb.io.meta, s1_fire) // Capture when BPU S1(PC_B) fires
private val s3_abtbMeta = RegEnable(s2_abtbMeta, s2_fire)
// → s3_abtbMeta.setIdx = setIndex(PC_A): ahead indexing preservation
```

> **Key**: `abtbMeta` is not sent as FTQ. Preserved only within BPU → Feedback to ABTB with fastTrain.

#### 11.14.4 Consumer predictor and t0_fire conditions

| Predictor | Activate | t0_fire condition | Remarks |
|---|---|---|---|
| **ABTB** (`abtb/AheadBtb.scala:206`) | Always | `enable && valid && taken && abtbMeta.valid` | When taken, only when meta is valid |
| **uBTB** (`ubtb/MicroBtb.scala:103`) | `UseFastTrain=true` (default) | `enable && valid` | no conditions taken; If false, replaced by mispred train |
| **uTAGE** (`utage/MicroTage.scala:127`) | `EnableFastTrain=true` (required) | `enable && valid` | conditional branch only internal filter |

#### 11.14.5 ABTB fastTrain training logic

**t0_fire condition interpretation:**
```
enable — Enable ABTB
&& fastTrain.valid — BPU S3 stage valid
&& finalPrediction.taken — S3 final prediction taken
(fallthrough = not-taken is the default → no learning required)
&& abtbMeta.valid — Did ABTB actually output the prediction in S1?
(ABTB miss → meta.valid=false → skip)
```

**T1 learning operation** (`abtb/AheadBtb.scala:217-304`):
```
t1_setIdx = abtbMeta.setIdx = setIndex(PC_A) ← Use ahead index as is
t1_bankMask = abtbMeta.bankMask

[Update taken counter] (by conditional branch entry)
position < trainPos → counter decrease (that branch is not actually taken)
position == trainPos → increment counter (matches actual taken branch)

[write entry]
!t1_hit: → Assign new entry
tag = getTag(fastTrain.startPc) = getTag(PC_B) ← ahead tag
    stored at setIndex(PC_A)

t1_hit && isIndirect && targetDiff → target modification (maintain tag/position)
```

#### 11.14.6 uBTB fastTrain training logic

uBTB is an always-taken predictor, so it is trained **regardless of whether it is taken or not**:

```
t0_fire = fastTrain.valid && enable (no taken condition)

By T1 case:
!hit: → Replace victim entry (select useful=0)
hit, mismatch or !actualTaken: → Decrease useful counter or reinitialize entry
hit, all matches, actualTaken: → increase useful counter (strengthen entry)
```

**Operates when FastTrain=false** (`ubtb/Parameters.scala`: can be changed to `UseFastTrain=false`):
```scala
// Replaced with regular train (only in case of misprediction)
t0_fire = io.stageCtrl.t0_fire && io.train.mispredictBranch.valid && io.enable
```

#### 11.14.7 uTAGE fastTrain learning logic

uTAGE only handles conditional branch directions and learns **after detecting misprediction**:

```
t0_fire = fastTrain.valid && enable

misprediction verdict:
t0_histHitMisPred = uTAGE hit, but (direction or cfiPosition) prediction was wrong
t0_histMissHitMisPred = uTAGE miss + prediction taken by S3 override

Learning decisions:
needAlloc = mispred → Allocate new entry (useful=0 overwrite victim)
needUpdate = hit → counter update

useful counter:
Increase: Accurate prediction && ABTB (base predictor) is incorrect ← uTAGE contributes to correction
reduction: misprediction
```

#### 11.14.8 Regular Train vs FastTrain comparison

| Item | General Train (`BpuTrain`) | FastTrain (`BpuFastTrain`) |
|---|---|---|
| **Source** | Backend → FTQ → BPU | BPU internal S3 |
| **Delay** | ~20~30 cycles | ~2~3 cycles |
| **Accuracy** | Actual execution result (ground truth) | S3 prediction (estimate) |
| **Target** | All predictors | S1 predictor only (uBTB, ABTB, uTAGE) |
| **Frequency** | When mispred + commit | Every S3 valid cycle |
| **Meta** | BpuResolveMeta (store and return FTQ) | abtbMeta + utageMeta (BPU internal retention) |

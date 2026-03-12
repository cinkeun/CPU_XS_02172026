# PHR (Path History Register) Analysis

> Analysis target: `src/main/scala/xiangshan/frontend/bpu/history/phr/`
> Code-based only — No use of prior knowledge/web-search

---

## Overview

PHR is the **path history register** that supplies history-based predictors (uTAGE, TAGE, ITTAGE, SC) in the XiangShan BPU.
Unlike a GHR (1-bit taken/not-taken shift register), PHR records the **hash of (PC, target) for each taken branch**.

| Item | Value |
|------|-------|
| History unit | 1 taken branch = `pathHash(pc, target)` → 15-bit value |
| Update condition | On taken branch only (not-taken causes no change) |
| Physical structure | Circular buffer (`Vec[Bool]` + pointer) |
| Consumers | uTAGE, TAGE, ITTAGE, SC path tables |

---

## Parameters (Configs.scala MinimalConfig)

```scala
// Source: bpu/history/phr/Parameters.scala
PhrParameters(
  Shamt          = 2,   // bits shifted in per taken branch
  PathHashWidth  = 15,  // pathHash output width
  HistoryAlign   = 4,   // PHR length rounded to this multiple
)
// PathHashHighWidth = PathHashWidth - Shamt = 13
```

```scala
// Source: frontend/FrontendParameters.scala:39-56
def getPhrHistoryLength: Int =
  nextMultipleOf(MaxTableHistoryLength + Shamt * FtqSize + FtqFullFix, HistoryAlign)
// MaxTableHistoryLength = max history length across TAGE, ITTAGE, SC-path tables
// Shamt * FtqSize = 2 * 8 = 16  (slack for FTQ-full overflow)
// FtqFullFix      = 4
```

---

## 1. pathHash — History Bit Generation

```scala
// Source: bpu/history/phr/Helpers.scala:60-63
def pathHash(pc: PrunedAddr, target: PrunedAddr): UInt = {
  val hash = Cat(pc(9, 1), 0.U(4.W)) ^ target(16, 2) // magic numbers
  hash(PathHashWidth - 1, 0)  // [14:0], 15 bits
}
```

| Input | Bits used | Treatment |
|-------|-----------|-----------|
| `pc` | `pc[9:1]` (9 bits) | Left-shifted 4 (`Cat(..., 0.U(4.W))`) |
| `target` | `target[16:2]` (15 bits) | XOR directly |

Result split:
- `shiftBits = hash[1:0]`  — 2 new bits to insert into PHR
- `hashHigh  = hash[14:2]` — 13 bits used for oldest-bit replacement and folded hash mixing

---

## 2. PHR Circular Buffer Structure

```scala
// Source: bpu/history/phr/Phr.scala:42-46
private val phr    = RegInit(0.U.asTypeOf(Vec(PhrHistoryLength, Bool())))
private val phrPtr = RegInit(0.U.asTypeOf(new PhrPtr))

private def getPhr(ptr: PhrPtr): UInt =
  (Cat(phr.asUInt, phr.asUInt) >> (ptr.value + 1.U))(PhrHistoryLength - 1, 0)
```

`phrPtr` points to the current head. `getPhr(ptr)` extracts `PhrHistoryLength` bits starting at `ptr+1` — **most recent branch is at MSB**.

---

## 3. PHR Update (on taken branch)

```scala
// Source: bpu/history/phr/Phr.scala:141-149
when(updateData.taken) {
  // write 2 new bits at ptr position
  phr[(ptr - 0)] := shiftBits(1)   // newest bit
  phr[(ptr - 1)] := shiftBits(0)

  // replace oldest bit positions (ptr+1 .. ptr+13) with hashHigh XOR saved snapshot
  for i in 1 to PathHashHighWidth:  // 13 iterations
    phr[(ptr + i)] := hashHigh(i-1) ^ phrLowBits(i-1)

  phrPtr := ptr - Shamt  // advance ptr by 2 (circular)
}
```

**On not-taken**: only `phrPtr` and low bits are restored; the raw buffer is not modified.

### Update Flow

```
taken branch occurs
      │
      ▼
pathHash(cfiPc, target)
  → shiftBits[1:0]  (2 new history bits)
  → hashHigh[14:2]  (13 bits for oldest-bit replacement + folded hash mixing)
      │
      ├─ phr[ptr]       ← shiftBits[1]
      ├─ phr[ptr-1]     ← shiftBits[0]
      ├─ phr[ptr+1..13] ← hashHigh XOR phrLowBits  (wrap-around oldest bit handling)
      └─ phrPtr -= 2
```

---

## 4. Folded History Incremental Update

Each predictor does not receive the raw PHR directly. Instead it receives `PhrFoldedHistory` instances pre-folded to the required `(HistoryLength, FoldedLength)` pairs.

Rather than recomputing from raw PHR every cycle, **incremental updates** are applied to the previous folded history.

```scala
// Source: bpu/history/phr/Bundles.scala:107-148
// PhrFoldedHistory.update(oldestBits, num=Shamt, shiftBits, hashHigh):
//
// 1. Circular shift left by num(=2)
// 2. XOR out oldest bits that wrap around (if histLen > foldedLen)
// 3. XOR in new shiftBits at MSB positions
// 4. XOR in computeFoldedHash(Cat(hashHigh, 0.U(2.W)), foldedLen)(histLen)
new foldedHist = circularShiftLeft(
  (old foldedHist)
  XOR (oldest_bits_exit at wrap-around positions)
  XOR (shiftBits at newest positions),
  num = 2
) XOR computeFoldedHash(Cat(hashHigh, 0.U(2.W)), foldedLen)(histLen)
```

`computeFoldedHash` splits `hashHigh` (13 bits) into `foldedLen`-sized chunks and XOR-folds them.

### s0_foldedPhr Selection Priority

```scala
// Source: bpu/history/phr/Phr.scala:163-203
when(redirect.valid)    → redirectData.foldedPhr   (recomputed from raw PHR)
.elsewhen(s3_override)  → s3_foldedPhrReg.update() (incremental)
.elsewhen(s1_valid)     → s1_foldedPhrReg.update() (incremental)
.otherwise              → s0_foldedPhrReg           (hold previous cycle)
```

---

## 5. PhrMeta — Prediction-Time Snapshot

PHR is updated speculatively, so the state at prediction time is saved in `PhrMeta` for recovery on misprediction.

```scala
// Source: bpu/history/phr/Bundles.scala:71-78
class PhrMeta extends PhrBundle {
  val phrPtr:     PhrPtr = new PhrPtr               // ptr at prediction time
  val phrLowBits: UInt   = UInt(PathHashHighWidth.W) // phr[ptr][12:0] snapshot (13 bits)
}
```

```scala
// Source: bpu/history/phr/Phr.scala:222-224
io.phrMeta.phrPtr     := s1_phrPtr
io.phrMeta.phrLowBits := s1_phrValue(PathHashHighWidth - 1, 0)  // phr[s1_ptr][12:0]
```

### Redirect Recovery

```scala
// Source: bpu/history/phr/Phr.scala:48-51
private def getRedirectPhr(phrMeta: PhrMeta): UInt = {
  val redirectErrorPhr = getPhr(phrMeta.phrPtr)  // read speculatively corrupted PHR
  Cat(redirectErrorPhr(PhrHistoryLength-1, PathHashHighWidth), phrMeta.phrLowBits)
  // upper bits from raw PHR, lower 13 bits replaced with saved snapshot
}
```

The recovered PHR is then passed to `computeFoldedHist` to rebuild all folded histories and drive `s0_foldedPhr`.

---

## 6. PHR Pipeline Outputs

```scala
// Source: bpu/history/phr/Phr.scala:226-230
io.s0_foldedPhr   := s0_foldedPhr       // uTAGE s0 prediction
io.s1_foldedPhr   := s1_foldedPhrReg    // TAGE s1 prediction
io.s2_foldedPhr   := s2_foldedPhrReg
io.s3_foldedPhr   := s3_foldedPhrReg    // s3 override PHR snapshot
io.trainFoldedPhr := metaPhrFolded       // commit-time train (via FTQ)
```

| Output | Consumer | Timing |
|--------|----------|--------|
| `s0_foldedPhr` | uTAGE (s0 combinational read) | Current predict cycle |
| `s1_foldedPhr` | TAGE (s1 SRAM read) | RegEnable from s0_fire |
| `s3_foldedPhr` | s3_overrideData.foldedPhr | RegEnable from s2_fire |
| `trainFoldedPhr` | BPU commit-time train | Recomputed from FTQ commit-time meta |

---

## 7. computeFoldedHist

Used at cold-start and after redirect recovery to compute folded history directly from a raw PHR value.

```scala
// Source: bpu/history/phr/Helpers.scala:65-75
def computeFoldedHist(phrValue: UInt, compLen: Int)(histLen: Int): UInt =
  if (PathHashWidth >= histLen):
    // take phrValue[histLen-1:0], split into compLen-sized chunks, XOR-fold
    ParallelXOR(phrValue[histLen-1:0].chunks(compLen))
  else:
    // PathHashWidth < histLen: fold the full phrValue width
    ParallelXOR(phrValue[PathHashWidth-1:0].chunks(compLen))
```

---

## Summary

| Step | Action |
|------|--------|
| **Bit generation** | `pathHash(pc, target)` → 15-bit: `shiftBits[1:0]` + `hashHigh[14:2]` |
| **Raw PHR update** | On taken: write `shiftBits` at `phr[ptr, ptr-1]`, replace `phr[ptr+1..13]` with `hashHigh XOR old`, `ptr -= 2` |
| **Folded update** | Incremental: circular shift + oldest-bit XOR out + newest-bit XOR in + hashHigh fold |
| **Redirect recovery** | `getRedirectPhr(phrMeta)` → reconstruct raw → `computeFoldedHist` |
| **Predictor supply** | `PhrAllFoldedHistories` — one `PhrFoldedHistory` per `(histLen, foldedLen)` pair |

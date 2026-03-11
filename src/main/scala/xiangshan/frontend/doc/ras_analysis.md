# RAS (Return Address Stack) Analysis Report

> **Analysis Principle**: code-based only. No web-search and prior knowledge-based inferences.

---

## 1.1 Summary

| Item | Content |
|---|---|
| core/repo/commit | XiangShan / `kunminghu-v3` / `bfbb21862` |
| RAS depth | specQueue=32, commitStack=16 |
| ITTAGE Existence | Yes. However, the condition `needIttage = isIndirect && !hasPop`. **Ret(isReturn=true) takes RAS priority, does not use ITTAGE** |
| Update method | **Hybrid** — speculative push/pop (based on S3-fire) + commit-time confirmation |
| Recovery method | **Checkpoint** — Save redirect meta (`ssp`, `sctr`, `tosw`, `tosr`, `nos`) to FTQ, restore pointers in batch when redirect, then re-push/pop |
| Recovery from consecutive prediction failures | Restore checkpoint for each redirect. However, recovery may be skipped if the redirect condition fails in `stackNearOverflow=true` state (refer to RAS-003) |
| Ret target priority | **RAS absolute priority**: If `isReturn=true`, use `ras.io.topRetAddr` unconditionally. ITTAGE is used only in `isIndirect && !hasPop` condition |
| Multi-fetch simultaneous call/ret | **Single-event**: Only one call or ret is processed in one cycle (`io.specIn.valid = s3_fire`, single event) |
| Top 3 core risks | ① Possibility of redirect suppression in case of overflow (RAS-003) ② `commitPushAddr = DontCare` (RAS-004) ③ Multiple call/ret not processed (RAS-005) |

---

## 1.2 Observation-based interface recording

### Ras module (`Ras.scala`)

| signal name | direction | Description |
|---|---|---|
| `io.specIn.valid` | Input | S3 fire. push/pop trigger |
| `io.specIn.bits.attribute.isCall` | Input | push or not |
| `io.specIn.bits.attribute.isReturn` | Input | pop or not |
| `io.specIn.bits.cfiPosition` | Input | instruction offset in fetch block |
| `io.specIn.bits.startPc` | Input | start block fetch PC |
| `io.topRetAddr` | Output | Current RAS top (return address). Used in S3 |
| `io.redirectMeta` | Output | checkpoint meta for redirect |
| `io.commitMeta` | Output | meta for commit |
| `io.redirect.valid` | Input | redirect/recovery trigger |
| `io.redirect.bits.attribute.isCall/isReturn` | Input | Whether to re-push/pop when redirecting |
| `io.redirect.bits.meta.ras` | Input | meta for checkpoint restoration |
| `io.commit.valid` | Input | commit confirmation |
| `io.commit.bits.attribute.isCall/isReturn` | Input | commit push/pop |

### MicroRas module (`MicroRas.scala`)

| signal name | direction | Description |
|---|---|---|
| `io.specIn.attribute.isCall/isReturn` | Input | S1 call/ret detection |
| `io.specIn.startPc`, `cfiPosition` | Input | push addr for calculation |
| `io.hasRedirect` | Input | global redirect signal |
| `io.hasOverride` | Input | S3 override signal |
| `io.fullRetAddr` | Input | Primary RAS top address (`ras.io.topRetAddr`) |
| `io.specOut.retTarget` | Output | Predicted return address provided to S1 |
| `io.specOut.isCanUse` | Output | Prediction Validity |

### RAS internal pointer (`RasStack.scala`)

| signal name | Description |
|---|---|
| `tosw` | Top of Stack write pointer (most recent push location in specQueue) |
| `tosr` | Top of Stack read pointer (current top read position) |
| `ssp` | Committed stack pointer (for speculative reference) |
| `sctr` | Stack counter (same address consecutive push compression counter, max=7) |
| `nsp` | Non-speculative committed stack pointer |
| `bos` | Bottom of Spec Queue pointer (commit baseline) |

---

## 1.3 Codification of operating rules

### Call determination rules

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/Bundles.scala:52,82-86
def isCall: Bool = rasAction === BranchAttribute.RasAction.Push
// hasPush:
// branchType === Direct && isLink(rd) && !isRVC (jal rd=x1/x5, uncompressed)
//   branchType === Indirect && isLink(rd)           (jalr rd=x1/x5)
// isLink(reg) = reg === 1 || reg === 5
```

- `jal` with `rd=x1` or `rd=x5` (uncompressed instruction, RVC `c.jal` is decoded as `c.addiw` in RV64)
- `jalr` with `rd=x1` or `rd=x5`
- `jalr` with `rd=x1/x5` AND `rs1=x1/x5` (PopAndPush: return-and-call)

### Return determination rules

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/Bundles.scala:53,124-125
def isReturn: Bool = rasAction === BranchAttribute.RasAction.Pop
// hasPop:
//   branchType === Indirect && isLink(rs) && rd =/= rs
```

- `jalr` with `rs1=x1` or `rs1=x5`, but with `rd≠rs1`

### Push rules

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:65-74
private val specPush = io.specIn.valid && io.specIn.bits.attribute.isCall
stack.spec.pushValid := specPush && !stackNearOverflow
private val specAlignPc = specIn.startPc & alignMask // Align by FetchBlockAlignWidth
private val specPushAddr = specAlignPc + (specIn.cfiPosition << 1.U).asUInt + 2.U
```

- If isCall is true during S3-fire, push occurs.
- Storage address: `(startPc & ~alignMask) + (cfiPosition * 2) + 2` → 2 byte address following call instruction (based on compressed ISA)
- Suppress push if `stackNearOverflow=true`

### Pop Rules

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:66,72
private val specPop = io.specIn.valid && io.specIn.bits.attribute.isReturn
stack.spec.popValid := specPop && !stackNearOverflow
```

- If isReturn is true during S3-fire, pop occurs.
- The return address is `timingTop.retAddr` (register calculated 1 cycle ahead)
- Pop suppression if `stackNearOverflow=true`

### Recovery rules when Flush/Redirect

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:93-114
private val redirect = RegNextWithEnable(io.redirect) // 1 cycle delay

stack.redirect.valid := redirect.valid && (isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow)
// Batch restore pointers
when(io.redirect.valid) {
  tosr := io.redirect.meta.tosr
  tosw := io.redirect.meta.tosw
  ssp  := io.redirect.meta.ssp
  sctr := io.redirect.meta.sctr
// If redirect is call, re-push, if ret, re-pop
}
```

- The redirect signal is processed after 1 cycle delay with `RegNextWithEnable`.
- Restore the saved `{ssp, sctr, tosw, tosr, nos}` checkpoint
- If redirect is a call command: additionally execute specPush after restoring the pointer
- When redirect is a ret command: additionally execute specPop after restoring the pointer
- **Warning**: If `stackNearOverflow=true` and `redirectTOSW >= stackTOSW`, redirect processing is skipped.

### Selection rules in case of conflict with ITTAGE/BTB

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/Bpu.scala:355-375
private val s3_useRas    = s3_firstTakenBranch.bits.attribute.isReturn
private val s3_useIttage = s3_firstTakenBranch.bits.attribute.needIttage && ittage.io.prediction.hit
// needIttage = isIndirect && !hasPop

s3_prediction.target := MuxCase(
  s3_fallThroughPrediction.target,
  Seq(
(s3_taken && s3_useRas) -> ras.io.topRetAddr, // top priority
    (s3_taken && s3_useIttage) -> ittage.io.prediction.target,
    s3_taken                   -> s3_firstTakenBranch.bits.target
  )
)
```

Priority (high → low):
1. `isReturn` → **RAS** (mutually exclusive with ITTAGE condition: if `hasPop`, then `needIttage=false`)
2. `isIndirect && !hasPop && ittage hit` → **ITTAGE**
3. `taken` → mBTB target
4. fallthrough

### Multiple call/ret simultaneous occurrence rules within the same fetch block

- **Process only one event in one cycle**: `io.specIn.valid = s3_fire` is a single signal
- `s3_prediction` uses the attribute of the **first branch taken** in the fetch block.
- Therefore, even if there is call+ret in the same fetch block, only the first branch taken is processed.
- `call_call`, `call_ret`, `ret_call`, `ret_ret`: Only the first event is reflected in RAS

---

## 1.4 Issue list

### RAS-001
- **ID**: RAS-001
- **Severity**: `Medium`
- **Symptom**: In `stackNearOverflow=true` state, both push and pop are suppressed, reducing RAS prediction accuracy.
- **reason**:
  ```scala
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:64,71-72
  private val stackNearOverflow = stack.specNearOverflow
  stack.spec.pushValid := specPush && !stackNearOverflow
  stack.spec.popValid  := specPop && !stackNearOverflow
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/RasStack.scala:414-418
  when(distanceBetween(tosw, bos) > (SpecQueueSize - 2).U) {
    specNearOverflowed := true.B
  }
  ```
- **Improvement suggestion**: Continue to allow pops even when overflowing, or consider cyclically overwriting overflow entries without suppressing pops.

### RAS-002
- **ID**: RAS-002
- **Severity**: `High`
- **Symptom**: When redirecting, if `stackNearOverflow=true` is `!isBefore(redirectTOSW, stackTOSW)`, redirect processing is skipped.
- **reason**:
  ```scala
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:99
  stack.redirect.valid := redirect.valid && (isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow)
  ```
- **Improvement suggestion**: Always allow redirect even when overflowing, or consider RAS reset immediately when entering overflow state.

### RAS-003
- **ID**: RAS-003
- **Severity**: `Medium`
- **Symptom**: When set to `commitPushAddr = DontCare` (Ras.scala:108), the actual push address is taken from `specQueue(metaTosw.value).retAddr`, but if the SpecQueueSize(32) limit is exceeded, the specQueue slot may be overwritten.
- **reason**:
  ```scala
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:108
  private val commitPushAddr = DontCare
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/RasStack.scala:352
  private val commitPushAddr = specQueue(io.commit.metaTosw.value).retAddr
  ```
- **Improvement suggestion**: Remove specQueue dependency by directly storing actual push address in FTQ.

### RAS-004
- **ID**: RAS-004
- **Severity**: `Medium`
- **Symptom**: Multiple call/ret simultaneous processing not supported. If there is call+ret in one fetch block, only the first event is processed.
- **Rationale**: `io.specIn.valid = s3_fire` is single valid, `s3_prediction` represents only one branch.
- **Improvement suggestion**: Introducing multiple event queues that process all calls/rets within a fetch block in order.

### RAS-005
- **ID**: RAS-005
- **Severity**: `Info`
- **Symptom**: Two commented `XSError` assertions — nsp-ssp mismatch, commit/spec address mismatch.
- **reason**:
  ```scala
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/RasStack.scala:349,371-373
  // XSError(io.commit.metaSsp =/= nsp, "nsp mismatch with expected ssp")
  // XSError(io.commit.pushAddr =/= commitPushAddr, "addr from commit mismatch with addr from spec")
  ```
- **Improvement Suggestions**: Identify the cause of annotation processing and fix it or reactivate it with conditional assertions.

### RAS-006
- **ID**: RAS-006
- **Severity**: `Low`
- **Symptom**: FIXME annotation for `bos` update — Condition `distanceBetween(io.commit.metaTosw, bos) > 2` occurs unexpectedly.
- **reason**:
  ```scala
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/RasStack.scala:381-385
  // FIXME: Currently this assertion fails. Fix or reconsider it in the future.
  // XSError(io.commit.valid && (distanceBetween(io.commit.metaTosw, bos) > 2.U), ...)
  ```
- **Improvement Suggestion**: Reexamine the BOS renewal policy and clarify whether it is an intentional condition or a bug.

---

## 1.5 Next prediction pseudocode

```text
onPredict(input: {startPc, s1_prediction, s3_prediction}, mainRasTop: PrunedAddr):

  // === S1 stage: MicroRas early prediction ===
  s1_specPush = s1_prediction.attribute.isCall
  s1_specPop  = s1_prediction.attribute.isReturn
  s1_pushAddr = getCfiPcFromPosition(specIn.startPc, specIn.cfiPosition) + 2

  // MicroRas decision (combinational, output is registered)
  if hasRedirect:
    isCanUse = false
  elif hasOverride (S3 override):
    // only S3 ops remain
    if s3_hasPop:   isCanUse = false
    if s3_hasPush:  isCanUse = true, retTarget = s3_retAddr
    else:           isCanUse = true, retTarget = mainRasTop
  elif s1_specPush:
    isCanUse = true, retTarget = s1_pushAddr
  elif s1_specPop:
    if s2_hasPush:  // S2 push cancels this pop
      if s3_hasPush:  isCanUse = true, retTarget = s3_retAddr
      elif s3_hasPop: isCanUse = false
      else:           isCanUse = true, retTarget = mainRasTop
    elif s2_hasPop:   isCanUse = false   // double pop
    elif s3_hasPush:  isCanUse = true, retTarget = mainRasTop
    else:             isCanUse = false   // main RAS popped, new top unknown
  else:
    // no CFI in S1
    if s2_hasPush:  isCanUse = true, retTarget = s2_retAddr
    elif s2_hasPop: isCanUse = s3_hasPush ? true : false
                    retTarget = s3_hasPush ? mainRasTop : 0
    elif s3_hasPop: isCanUse = false
    elif s3_hasPush: isCanUse = true, retTarget = s3_retAddr
    else:           isCanUse = !redirectDelay1, retTarget = mainRasTop

  // Apply MicroRas to S1 prediction
  if s1_prediction.attribute.isReturn && isCanUse:
    s1_prediction.target = retTarget   // override BTB target

  // === S3 stage: Main RAS (commit-eligible prediction) ===
  // (fires on s3_fire via io.specIn)
  s3_useRas    = s3_firstTakenBranch.attribute.isReturn
  s3_useIttage = s3_firstTakenBranch.attribute.needIttage && ittage.hit

  // Push/Pop (speculative)
  if s3_fire && isCall && !stackNearOverflow:
    alignedPc = s3_startPc & alignMask
    pushAddr  = alignedPc + (cfiPosition << 1) + 2
    push(pushAddr) → updates specQueue[tosw], advances tosw, ssp/sctr updated

  if s3_fire && isReturn && !stackNearOverflow:
    pop()  → tosr updates to NOS, ssp/sctr updated
             timingTop pre-computed for next cycle

  // Target selection (priority: RAS > ITTAGE > mBTB target)
  if s3_taken && s3_useRas:
    target = ras.io.topRetAddr   // = timingTop.retAddr (registered)
  elif s3_taken && s3_useIttage:
    target = ittage.prediction.target
  elif s3_taken:
    target = s3_firstTakenBranch.target
  else:
    target = fallThrough.target

  output: {target, taken, cfiPosition, attribute, redirectMeta}

  // === Redirect recovery ===
  // (fires 1 cycle after io.redirect.valid, via RegNextWithEnable)
  on redirect (if isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow):
    restore {ssp, sctr, tosw, tosr} from saved meta
    if redirect.isCall: re-push(redirect.cfiPc + 2)
    if redirect.isRet:  re-pop()
    writeBypass cleared/updated accordingly
```

---

## 1.6 Input-to-output latency and throughput

| Unit | Input Stage | Output Stage | Latency(cycle) | Throughput(pred/cycle) |
|---|---|---|---|---|
| MicroRas (S1 early) | S1 (`s1_fire`) | S1 (next cycle, registered output) | 1 cycle (prev S1→current S1) | 1 pred/cycle |
| Main RAS push/pop | S3 (`s3_fire`) | S3 (timingTop pre-computed, available next S3) | 0 cycles (within S3 via registered `timingTop`) | 1 pred/cycle |
| Main RAS top read | — | S3 (`ras.io.topRetAddr`) | 0 (combinational from `timingTop` register) | — |
| Redirect recovery | redirect.valid | redirect+2 cycles (RegNextWithEnable + 1) | 2 cycles | — |

**reference**:
- `timingTop` is a register, but updated in the **same cycle** as the push/pop event (non-blocking assignment)
- In reality, “topRetAddr is reflected in the next cycle after the previous S3 push/pop event.”
- MicroRas provides results in S1, so early prediction is 2 cycles faster than S3

---

## 1.7 Pipeline stage location

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
|---|---|---|---|
| `startPc` | S0 (`s0_startPc`) | S1 (`s1_startPc = RegEnable(s0_startPc, s0_fire)`) | S0→S1 1 cycle |
| `s1_prediction.attribute` | S1 (combinational from uBTB/abtb) | S1 (MicroRas input), S2 (RegEnable) | MicroRas input at S1 |
| `uras.io.specOut.isCanUse/retTarget` | registered at end of S1 | S1 (combinational use: `when(s1_isRet && uras.io.specOut.isCanUse)`) | S1 output is previous-cycle S1 computation |
| `ras.io.topRetAddr` | S3 (timingTop register updated) | S3 (s3_prediction MuxCase) | Available at S3, updated by previous S3 event |
| `ras.io.redirectMeta` | S3 (`stack.meta.*`) | FTQ (via `io.toFtq.meta`) | Stored in FTQ `redirectMeta.ras` field |
| `ras.io.commitMeta` | S3 (`stack.meta.ssp/tosw`) | FTQ (via `io.toFtq.meta`) | Stored in FTQ `commitMeta.ras` field |
| `redirect/flush` (from FTQ) | FTQ (backend resolve) | Ras (RegNextWithEnable delay=1) | 1 cycle delay in Ras; BPU sees raw `redirect.valid` for MicroRas |
| `io.specIn.valid` (Ras) | Bpu: `s3_fire` | Ras push/pop, timingTop update | S3 only |

---

## 1.8 Training method

### Training trigger summary

| Trigger | Predictor | input | speculative or not |
|---|---|---|---|
| S3-fire (speculative) | RAS (push/pop) | `startPc`, `cfiPosition`, `attribute` | Speculative |
| redirect (mispredict) | RAS (restore+redo) | checkpoint meta `{ssp,sctr,tosw,tosr,nos}`, `cfiPc+2` | Recovery |
| commit-valid | commitStack update | `attribute.isCall/isReturn`, `metaSsp`, `metaTosw` | Non-speculative |

### Training meta fields

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/Bundles.scala:76-78
class RasRedirectMeta(implicit p: Parameters) extends RasInternalMeta {
  val topRetAddr: PrunedAddr = PrunedAddr(VAddrBits)
}
// RasInternalMeta:
//   ssp:  UInt(log2Up(CommitStackSize).W)  // committed stack pointer
//   sctr: UInt(StackCounterWidth.W)         // stack counter (3-bit)
//   tosw: RasPtr                            // write pointer
//   tosr: RasPtr                            // read pointer
//   nos:  RasPtr                            // next-of-stack pointer

// Source: src/main/scala/xiangshan/frontend/bpu/ras/Bundles.scala:80-83
class RasCommitMeta(implicit p: Parameters) extends RasBundle {
  val ssp:  UInt   = UInt(log2Up(CommitStackSize).W)
  val tosw: RasPtr = new RasPtr
}
```

#### 1.8.1 training trigger details

**resolve/mispredict based**:
- RAS recovery when receiving `io.redirect` (BpuRedirect)
- Restore checkpoint to `redirect.bits.meta.ras` field
- Re-push if redirect.isCall, re-pop if redirect.isRet
- RAS itself does not learn separate tables — pure stack restoration

**commit-based**:
- Update commitStack when receiving `io.commit.valid` (RegNext delay 1 cycle)
- push: Read address from `specQueue(metaTosw.value).retAddr` and write to commitStack
- pop: commitStack[nsp].ctr reduction, nsp adjustment

**fast-train (based on S3 override)**:
- MicroRas receives the S3 override signal and immediately resets the S1-S2 in-flight state.

#### 1.8.2 FTQ storage information

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/Bundles.scala:261-275
class BpuRedirectMeta(implicit p: Parameters) extends BpuBundle {
  val ras: RasRedirectMeta = new RasRedirectMeta  // ssp, sctr, tosw, tosr, nos, topRetAddr
}
class BpuCommitMeta(implicit p: Parameters) extends BpuBundle {
  val ras: RasCommitMeta = new RasCommitMeta      // ssp, tosw
}
```

| Storage information | Field | Use |
|---|---|---|
| `ssp` (speculative stack ptr) | redirectMeta.ras.ssp | Restore on redirect |
| `sctr` (stack counter) | redirectMeta.ras.sctr | Restore on redirect |
| `tosw` (write ptr) | redirectMeta.ras.tosw, commitMeta.ras.tosw | Used when redirect/commit |
| `tosr` (read ptr) | redirectMeta.ras.tosr | Restore on redirect |
| `nos` (next-of-stack ptr) | redirectMeta.ras.nos | Calculate next top when redirect pop |
| `topRetAddr` | redirectMeta.ras.topRetAddr | (Stored, but not confirmed for actual use) |
| `ssp` (commit) | commitMeta.ras.ssp | nsp correction when commit |
| `tosw` (commit) | commitMeta.ras.tosw | For querying commitStack address |

#### 1.8.3 Storage location within FTQ

- `BpuMeta.redirectMeta.ras` (RasRedirectMeta): **Save FTQ entry field directly**
- `BpuMeta.commitMeta.ras` (RasCommitMeta): **Save FTQ entry field directly**
- No separate meta RAM — inline storage within FTQ entry

#### 1.8.4 Write port congestion handling

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/RasStack.scala:305-313
realPush := RegNext(io.spec.pushValid, init = false.B) || RegNext(...)
when(realPush) {
  specQueue(realWriteAddr.value) := realWriteEntry
  specNos(realWriteAddr.value)   := realNos
}
```

- specQueue actual write (`realPush`) is delayed by **1 cycle** from push event
- bypass to `writeBypassEntry/writeBypassValid` during delay
- Pointer (tosw/tosr/ssp/sctr) updates are immediate (same cycle)
- **single write port**: No arbiter, only one push event processed in one cycle

---

## 2) Memory structure analysis

### 2.1 Memory Spec

| Memory/Table | Depth | Width(bit) | #Tables | Banks | Read Ports | Write Ports |
|---|---|---|---|---|---|---|
| specQueue (RasEntry Vec) | 32 | retAddr(VAddrBits-pruned) + ctr(3) | 1 | 1 | 1 (combinational) | 1 (1-cycle delayed) |
| specNos (RasPtr Vec) | 32 | log2Ceil(32)+1 = 6 | 1 | 1 | 1 | 1 |
| commitStack (RasEntry Vec) | 16 | retAddr(VAddrBits-pruned) + ctr(3) | 1 | 1 | 1 | 1 |

```
// Source: src/main/scala/xiangshan/frontend/bpu/ras/Parameters.scala:22-27
case class RasParameters(
    CommitStackSize:   Int = 16,
    SpecQueueSize:     Int = 32,  // must be pow2
    StackCounterWidth: Int = 3    // max counter = 7
)
```

### 2.2 Memory update/recovery path

**Speculative push path**:
1. Activate `io.spec.pushValid` during S3-fire
2. Immediate update of pointers (tosw/ssp/sctr)
3. `writeBypassEntry` immediate update (bypass for reading next cycle)
4. `realPush = RegNext(pushValid)` → Actually write specQueue in the next cycle

**Speculative pop path**:
1. Activate `io.spec.popValid` during S3-fire
2. Update `tosr` with reference to `topNos`
3. `timingTop` register: Pre-calculate and store the next top

**Flush/redirect recovery path**:
1. Receive `redirect.valid` → 1 cycle delay to `RegNextWithEnable`
2. `stack.redirect.valid` condition evaluation
3. `tosr/tosw/ssp/sctr` batch restore
4. If isCall, push, if isRet, pop is additionally executed.
5. `writeBypass` Update

**Commit confirmation path**:
1. 1 cycle delay from `io.commit.valid` → `RegNext`
2. If pushValid: Read address from specQueue[metaTosw], update commitStack[nsp], advance nsp
3. If popValid: commitStack[nsp].ctr decrement, nsp backward
4. `bos` update: `bos := metaTosw` when commit push

---

## 3) RAS Entry definition

### 6.1 Entry definition code snippet

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/Bundles.scala:27-39
class RasEntry(implicit p: Parameters) extends RasBundle {
  val retAddr: PrunedAddr = PrunedAddr(VAddrBits)
  val ctr:     UInt       = UInt(StackCounterWidth.W)  // StackCounterWidth=3
}
```

### 6.2 Entry field description

| Field Name | Width(bit) | Description |
|---|---|---|
| `retAddr` | VAddrBits (pruned) | return address. pruning some of the upper bits with `PrunedAddr` |
| `ctr` | 3 | Compression counter for consecutive pushes with the same return address. 0 = 1 time, 7 = 8 times (max) |

**Compression operation**: When the same `retAddr` is pushed consecutively, ctr is incremented and accumulated in the existing slot without allocating a new slot. When popping, if ctr > 0, only ctr is decreased, and if ctr = 0, the actual slot is released.

---

## 4) Multiple call/ret simultaneous processing rules

| case | Processing method | Results |
|---|---|---|
| `call_call` (same fetch block) | Process only the first taken branch | Second call unprocessed → return address stack incomplete |
| `call_ret` | If call is first taken, only call is processed | ret unprocessed |
| `ret_call` | If ret is the first taken, only ret is processed | call unprocessed |
| `ret_ret` | Process only the first ret | Second ret unprocessed |
| Same slot call+ret (PopAndPush) | `isReturnAndCall = rasAction === PopAndPush` | **In Ras.scala, only isCall/isReturn is checked** — You need to clearly check in the code whether PopAndPush is processed properly |

**Notice**: `BranchAttribute.PopAndPush` (`RasAction.PopAndPush = 0b11`) exists, but the condition of `Ras.scala` is:
```scala
private val specPush = io.specIn.valid && io.specIn.bits.attribute.isCall
private val specPop  = io.specIn.valid && io.specIn.bits.attribute.isReturn
// isCall = rasAction === Push (0b10)
// isReturn = rasAction === Pop (0b01)
// If PopAndPush (0b11), isCall=false, isReturn=false → Neither is processed!
```

**RAS-007** (Critical):
- **Severity**: `High`
- **Symptom**: Neither push nor pop occurs when processing the `PopAndPush (0b11)` (return-and-call: jalr rs1=x1/x5, rd=x1/x5, rs1≠rd) command.
- **reason**:
  ```scala
  // Bundles.scala:54: def isReturnAndCall: Bool = rasAction === RasAction.PopAndPush
  // Ras.scala:65-66:
  private val specPush = io.specIn.valid && io.specIn.bits.attribute.isCall      // isCall = Push(0b10) only
  private val specPop  = io.specIn.valid && io.specIn.bits.attribute.isReturn    // isReturn = Pop(0b01) only
  // PopAndPush(0b11) is neither
  ```
- **Improvement suggestion**: Change to use the `hasPush/hasPop` bit field:
  ```scala
  private val specPush = io.specIn.valid && io.specIn.bits.attribute.hasPush
  private val specPop  = io.specIn.valid && io.specIn.bits.attribute.hasPop
  ```

---

## 5) Verification (testing) standards

### Base case

- [x] Basic call/ret — ret pop after call push
- [x] nested call/ret — push 2 times and then pop 2 times
- [ ] Overflow boundary — specNearOverflow=true, suppress push/pop when calling exceeds SpecQueueSize(32)
- [ ] underflow — commitStack fallback action when pops exceed pushes
- [ ] redirect adjacent cycle ret — Ensures that the ret of the cycle immediately after the redirect uses the correct address.
- [ ] PopAndPush (return-and-call) command processing (currently unprocessed confirmation required)

### Multi-event case

| case | Expected Behavior | Verification points |
|---|---|---|
| `call_call` same block | push only the first call | Check for missing second call ret addr |
| `call_ret` same block | Process only calls (based on first taken) | Which branch is selected in S3 |
| `ret_ret` same block | pop only the first ret | Second ret mispredict incidence |
| `ret_call` same block | Process only ret | Same |

### RAS/ITTAGE priority cases

- [ ] `isReturn=true` Only use RAS when predicting branch, check ITTAGE is ignored
- [ ] Confirm the use of ITTAGE when ITTAGE is hit in the `isIndirect && !hasPop` branch

### Overflow/Recovery Case

- [ ] redirect during overflow → `stack.redirect.valid` suppression condition verification
- [ ] Check `isCanUse=false` for 1 cycle immediately after MicroRas redirect (`redirectDelay1`)

### Commit port case

- [ ] Continuous commit push → Check address accuracy when reusing specQueue slot
- [ ] commitStack single write port — ensure no concurrent pushes/pops (only one of them active)

---

## 6) Feature Checklist

- [x] Call/Ret discrimination accuracy — Check implementation based on RISC-V spec Table 3
- [x] Push address calculation — `alignedPC + (cfiPosition<<1) + 2` (based on compressed ISA 2 bytes)
- [x] Speculative update — S3-fire based immediate pointer update
- [x] Redirect rollback — checkpoint-based pointer restoration + re-push/pop
- [~] Overflow handling — push/pop suppression during specNearOverflow (redirect suppression problem exists)
- [x] ITTAGE concurrent hit priority — RAS > ITTAGE clearly implemented
- [x] Maintain Commit stack separation
- [!] PopAndPush (return-and-call) — Currently unhandled (RAS-007)
- [!] Multiple call/ret simultaneous processing — not supported (single event only)
- [x] Recovery from consecutive prediction failures — Restore to checkpoint for each redirect (beware of overflow exception)

---

*Analysis standard file: `src/main/scala/xiangshan/frontend/bpu/ras/` all + `Bpu.scala` + `Bundles.scala`*
*Code commit: `bfbb21862` (branch: `kunminghu-v3`)*

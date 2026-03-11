# Full RAS (Ras + RasStack) analysis report

> **Analysis Principle**: code-based only. No web-search and prior knowledge-based inferences.
> **Analysis target**: `src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala` + `RasStack.scala` + `Bundles.scala` + `Parameters.scala`
> **Core/Repo/Commit**: XiangShan / `kunminghu-v3` / `bfbb21862`

---

## 1. Structure overview

Full RAS consists of two modules:

| module | file | role |
|---|---|---|
| `Ras` | `Ras.scala` | Top rapper. BPU pipeline and interface. push/pop/redirect/commit routing |
| `RasStack` | `RasStack.scala` | Real memory (specQueue, commitStack) and pointer management logic |

Operation pipeline location: **S3** (based on `io.specIn.valid = s3_fire`)

---

## 2. Parameters

```scala
// Source: Parameters.scala:21-27
case class RasParameters(
CommitStackSize: Int = 16, // commitStack depth
SpecQueueSize: Int = 32, // specQueue depth (must be pow2)
StackCounterWidth: Int = 3 // ctr bit width → max counter = 7
)
```

| parameters | value | Description |
|---|---|---|
| `CommitStackSize` | 16 | Committed call/ret history stack depth |
| `SpecQueueSize` | 32 | speculative push entry queue depth |
| `StackCounterWidth` | 3 | Same address consecutive push compression counter width (max=7) |

---

## 3. Memory structure

### 3.1 Entry definition

```scala
// Source: Bundles.scala:27-30
class RasEntry(implicit p: Parameters) extends RasBundle {
val retAddr: PrunedAddr = PrunedAddr(VAddrBits) // return address (pruning high-order bits)
val ctr: UInt = UInt(StackCounterWidth.W) // Continuous push compression counter
}
```

**ctr compression behavior**:
- `ctr++` without a new slot when the same `retAddr` is pushed consecutively
- When popping, if `ctr > 0`, only `ctr--`, if `ctr == 0`, the slot is released.

### 3.2 Memory table

| memory | Type | Depth | Width | Read port | Write port |
|---|---|---|---|---|---|
| `specQueue` | `Vec[RasEntry]` | 32 | `retAddr + ctr(3)` | 1 (combinational) | 1 (1-cycle delayed) |
| `specNos` | `Vec[RasPtr]` | 32 | 6-bit (ptr) | 1 | 1 |
| `commitStack` | `Vec[RasEntry]` | 16 | `retAddr + ctr(3)` | 1 | 1 |

```scala
// Source: RasStack.scala:67-69
private val commitStack = RegInit(VecInit(Seq.fill(CommitStackSize)(...)))
private val specQueue   = RegInit(VecInit(Seq.fill(SpecQueueSize)(...)))
private val specNos     = RegInit(VecInit(Seq.fill(SpecQueueSize)(...)))
```

---

## 4. Pointer definition

```scala
// Source: RasStack.scala:71-78
private val nsp  = RegInit(0.U(log2Up(CommitStackSize).W))  // commit stack pointer
private val ssp  = RegInit(0.U(log2Up(CommitStackSize).W))  // speculative committed stack pointer
private val sctr = RegInit(0.U(StackCounterWidth.W))         // speculative counter
private val tosr = RegInit(RasPtr(true.B, (SpecQueueSize-1).U)) // spec queue read ptr
private val tosw = RegInit(RasPtr(false.B, 0.U))             // spec queue write ptr
private val bos = RegInit(RasPtr(false.B, 0.U)) // bottom of spec queue (commit baseline)
```

| pointer | Description |
|---|---|
| `nsp` | Current top pointer of the commit stack (non-speculative) |
| `ssp` | Speculative reference pointer in commit stack |
| `sctr` | Same address consecutive push counter (speculative) |
| `tosw` | specQueue write pointer (next to latest push position) |
| `tosr` | specQueue read pointer (current top read position) |
| `bos` | Lower limit of confirmed commits in specQueue (Bottom of Spec) |

---

## 5. Ras module interface (parent)

```scala
// Source: Ras.scala:44-51
val specIn:   Valid[RasSpecInfo] = Flipped(Valid(new RasSpecInfo))
val commit:   Valid[BpuCommit]   = Flipped(Valid(new BpuCommit))
val redirect: Valid[BpuRedirect] = Flipped(Valid(new BpuRedirect))
val topRetAddr:   PrunedAddr      = Output(PrunedAddr(VAddrBits))
val redirectMeta: RasRedirectMeta = Output(new RasRedirectMeta)
val commitMeta:   RasCommitMeta   = Output(new RasCommitMeta)
```

| signal name | direction | Timing | Description |
|---|---|---|---|
| `specIn.valid` | Input | `s3_fire` | push/pop trigger |
| `specIn.bits.attribute.isCall` | Input | S3 | push or not |
| `specIn.bits.attribute.isReturn` | Input | S3 | pop or not |
| `specIn.bits.cfiPosition` | Input | S3 | CFI offset in fetch block |
| `specIn.bits.startPc` | Input | S3 | start block fetch PC |
| `topRetAddr` | Output | S3 | Current top return address (`timingTop.retAddr`) |
| `redirectMeta` | Output | S3 | checkpoint for FTQ storage (`ssp,sctr,tosw,tosr,nos,topRetAddr`) |
| `commitMeta` | Output | S3 | commit meta for FTQ storage (`ssp,tosw`) |
| `redirect.valid` | Input | — | mispredict recovery trigger |
| `redirect.bits.meta.ras` | Input | — | checkpoint to restore |
| `commit.valid` | Input | — | commit confirmation |

---

## 6. Push operation (Speculative)

```scala
// Source: Ras.scala:59-74
def alignMask: UInt = ((~0.U(VAddrBits.W)) << FetchBlockAlignWidth).asUInt
private val specPush     = io.specIn.valid && io.specIn.bits.attribute.isCall
private val specAlignPc  = specIn.startPc & alignMask
private val specPushAddr = specAlignPc + (specIn.cfiPosition << 1.U).asUInt + 2.U
stack.spec.pushValid := specPush && !stackNearOverflow
```

**Address calculation**: `(startPc & alignMask) + (cfiPosition * 2) + 2`
- `alignMask`: `FetchBlockAlignWidth` bitwise alignment mask
- `cfiPosition * 2`: 2 byte unit offset (RVC standard)
- `+ 2`: CALL instruction next address (2 byte units)

**Suppression Condition**: Block push if `stackNearOverflow = true`

### Inside RasStack.specPush

```scala
// Source: RasStack.scala:132-149
def specPush(retAddr, currSsp, currSctr, currTosr, currTosw, topEntry): Unit = {
  tosr := currTosw
  tosw := specPtrInc(currTosw)
  when(topEntry.retAddr === retAddr && currSctr < StackCounterMax.U) {
sctr := currSctr + 1.U // Same address: only increment ctr
  }.otherwise {
    ssp  := ptrInc(currSsp)
sctr := 0.U // New address: ssp advance + ctr reset
  }
}
```

**Write actual specQueue**:
```scala
// Source: RasStack.scala:305-313
realPush := RegNext(io.spec.pushValid, init = false.B) || RegNext(io.redirect.valid && io.redirect.isCall, ...)
when(realPush) {
  specQueue(realWriteAddr.value) := realWriteEntry
  specNos(realWriteAddr.value)   := realNos
}
```
- Pointer update: **immediately** (in push event cycle)
- specQueue memory write: **1 cycle delay** (`RegNext`)
- Delay period: bypass to `writeBypassEntry/writeBypassValid`

---

## 7. Pop operation (Speculative)

```scala
// Source: Ras.scala:66,72
private val specPop = io.specIn.valid && io.specIn.bits.attribute.isReturn
stack.spec.popValid := specPop && !stackNearOverflow
```

**Suppression condition**: Block pop if `stackNearOverflow = true`

### Inside RasStack.specPop

```scala
// Source: RasStack.scala:151-169
def specPop(currSsp, currSctr, currTosr, currTosw, currTopNos): Unit = {
  when(tosrInRange(currTosr, currTosw)) {
tosr := currTopNos // Entry in specQueue → Move to NOS
  }
  when(currSctr > 0.U) {
sctr := currSctr - 1.U // Decrement only the counter
  }.elsewhen(tosrInRange(currTopNos, currTosw)) {
    ssp  := ptrDec(currSsp)
sctr := specQueue(currTopNos.value).ctr // Use in-flight data
  }.otherwise {
    ssp  := ptrDec(currSsp)
sctr := getCommitTop(ptrDec(currSsp)).ctr // commit data fallback
  }
}
```

---

## 8. timingTop: Calculate output address

```scala
// Source: RasStack.scala:220-287
private val timingTop = RegInit(0.U.asTypeOf(new RasEntry))
```

`timingTop` is a register that **pre-calculates the top required for the next cycle**.
Updates every cycle with the following priorities:

| Conditions | `timingTop` update value |
|---|---|
| `writeBypassValidWire && (redirect.isCall || spec.pushValid)` | `writeEntry` (entry just pushed) |
| `writeBypassValidWire` (bypass only) | `writeBypassEntry` |
| `redirect.valid && redirect.isRet` | NOS-based top calculation after redirect restore |
| `redirect.valid` (non-call/ret) | redirect meta based top |
| `spec.popValid` | NOS-based next top calculation |
| `realPush` | `realWriteEntry` |
| otherwise | Current pointer-based `getTop()` |

```scala
// Source: RasStack.scala:323
io.spec.popAddr := timingTop.retAddr
// Ras.scala:91
io.topRetAddr := stack.spec.popAddr
```

---

## 9. Redirect/recovery operation

```scala
// Source: Ras.scala:93-104
private val redirect = RegNextWithEnable(io.redirect) // 1 cycle delay
stack.redirect.valid := redirect.valid && (isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow)
stack.redirect.meta  := redirect.bits.meta.ras
stack.redirect.callAddr := redirect.bits.cfiPc + 2.U
```

**1. redirect signal delay**: `RegNextWithEnable` → delivered to RasStack after 1 cycle
**2. Processing condition**: `isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow`
- If it is overflow and redirectTOSW >= stackTOSW, **processing is skipped** → Issue RAS-002

```scala
// Source: RasStack.scala:387-412
when(io.redirect.valid) {
tosr := io.redirect.meta.tosr // Batch restore of pointers
  tosw := io.redirect.meta.tosw
  ssp  := io.redirect.meta.ssp
  sctr := io.redirect.meta.sctr

  when(io.redirect.isCall) { specPush(...) }  // re-push
  when(io.redirect.isRet)  { specPop(...)  }  // re-pop
}
```

**Recovery Sequence**:
1. Restore checkpoint pointer (`tosr, tosw, ssp, sctr`)
2. If redirect is CALL: re-push (`callAddr = cfiPc + 2`)
3. If redirect is RET: re-pop
4. `writeBypass` Update

---

## 10. Commit operation

```scala
// Source: Ras.scala:106-114
private val commitValid = RegNext(io.commit.valid, init = false.B) // 1 cycle delay
private val commitInfo  = RegEnable(io.commit.bits, io.commit.valid)
stack.commit.valid     := commitValid
stack.commit.pushValid := commitValid && commitInfo.attribute.isCall
stack.commit.popValid  := commitValid && commitInfo.attribute.isReturn
stack.commit.pushAddr := DontCare // ← Look up specQueue inside RasStack for actual address
stack.commit.metaTosw  := commitInfo.meta.ras.tosw
stack.commit.metaSsp   := commitInfo.meta.ras.ssp
```

**commit push path** (RasStack.scala:352-374):
```scala
private val commitPushAddr = specQueue(io.commit.metaTosw.value).retAddr // Look up address in specQueue
when(io.commit.pushValid) {
  when(commitTop.ctr < StackCounterMax.U && commitTop.retAddr === commitPushAddr) {
commitStack(nspUpdate).ctr := commitTop.ctr + 1.U // compress ctr
  }.otherwise {
    nsp := ptrInc(nspUpdate)
    commitStack(ptrInc(nspUpdate)).retAddr := commitPushAddr
    commitStack(ptrInc(nspUpdate)).ctr     := 0.U
  }
}
```

**commit pop path** (RasStack.scala:333-350):
```scala
when(io.commit.popValid) {
  when(commitTop.ctr > 0.U) {
    commitStack(nspUpdate).ctr := commitTop.ctr - 1.U
  }.otherwise {
    nsp := ptrDec(nspUpdate)
  }
}
```

**nsp correction**:
- Force `nsp := metaSsp` when `io.commit.metaSsp =/= nsp` (prevents error accumulation)
- XSError assertion commented out (issue FRAS-003)

---

## 11. specNearOverflow

```scala
// Source: RasStack.scala:414-418
when(distanceBetween(tosw, bos) > (SpecQueueSize - 2).U) {
  specNearOverflowed := true.B
}.otherwise {
  specNearOverflowed := false.B
}
```

- If the distance between `tosw` and `bos` exceeds `SpecQueueSize - 2 = 30`, it is in an overflow state.
- **push blocking + pop blocking** occurs simultaneously (Ras.scala:71-72)
- Possibility to suppress redirect (Ras.scala:99)

---

## 12.bos update

```scala
// Source: RasStack.scala:376-380
when(io.commit.pushValid) {
  bos := io.commit.metaTosw
}.elsewhen(io.commit.valid && (distanceBetween(io.commit.metaTosw, bos) > 2.U)) {
  bos := specPtrDec(io.commit.metaTosw)
}
```

- When commit push: `bos := metaTosw`
- If the commit is valid but not push but `distanceBetween > 2`: `bos := metaTosw - 1`
- FIXME annotated XSError (issue FRAS-005)

---

## 13. Checkpoint (FTQ storage information)

### redirectMeta (RasRedirectMeta)

```scala
// Source: Bundles.scala:56-78
class RasInternalMeta {
  val ssp:  UInt   // committed stack pointer
  val sctr: UInt   // stack counter
  val tosw: RasPtr // write pointer
  val tosr: RasPtr // read pointer
  val nos:  RasPtr // next-of-stack pointer
}
class RasRedirectMeta extends RasInternalMeta {
val topRetAddr: PrunedAddr // current top return address (= timingTop at S3)
}
```

| field | Use |
|---|---|
| `ssp` | Restoring committed stack pointer when redirecting |
| `sctr` | Restore counters on redirect |
| `tosw` | Restore write pointer when redirecting; Check specQueue address when commit |
| `tosr` | Restore read pointer when redirecting |
| `nos` | Calculate next top when redirect pop |
| `topRetAddr` | Saved but not confirmed for use |

### commitMeta (RasCommitMeta)

```scala
// Source: Bundles.scala:80-83
class RasCommitMeta {
val ssp: UInt // for nsp correction when committing
val tosw: RasPtr // For specQueue address lookup
}
```

---

## 14. writeBypass mechanism

SpecQueue writing is delayed by 1 cycle, so a bypass is needed to read the next cycle immediately after push:

```scala
// Source: RasStack.scala:81-85
private val writeBypassEntry = Reg(new RasEntry)
private val writeBypassNos   = Reg(new RasPtr)
private val writeBypassValid = RegInit(0.B)
```

**Enable/Disable**:
- push or redirect+call → `writeBypassValid = true`, `writeBypassEntry = writeEntry`
- redirect (non-call) → `writeBypassValid = false` (clear)
- `spec.fire` but not push → `writeBypassValid = false`

**use**:
- `getTop()`: If `allowBypass=true`, bypass is used first when `writeBypassValid`
- Priority processing based on `writeBypassValidWire` in `timingTop` calculations

---

## 15. Issue list

### FRAS-001
- **Severity**: `Medium`
- **Symptom**: Both push and pop are suppressed during `stackNearOverflow=true`
- **reason**:
  ```scala
  // Ras.scala:71-72
  stack.spec.pushValid := specPush && !stackNearOverflow
  stack.spec.popValid  := specPop && !stackNearOverflow
  ```
- **Improvement suggestion**: Allow pop even when overflowing or consider circular overwriting method

### FRAS-002
- **Severity**: `High`
- **Symptom**: `stackNearOverflow=true` + `redirectTOSW >= stackTOSW` → Skip redirect processing
- **reason**:
  ```scala
  // Ras.scala:99
  stack.redirect.valid := redirect.valid && (isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow)
  ```
- **Improvement suggestion**: Always allow redirect even in overflow, or consider RAS reset immediately when overflow occurs.

### FRAS-003
- **Severity**: `Medium`
- **Symptom**: The commit push address is sent to `DontCare` and RasStack looks it up directly in the specQueue. If specQueue slots exceed 32 and are overwritten, incorrect address commit may occur.
- **reason**:
  ```scala
  // Ras.scala:108
  private val commitPushAddr = DontCare
  // RasStack.scala:352
  private val commitPushAddr = specQueue(io.commit.metaTosw.value).retAddr
  ```
- **Improvement suggestion**: Remove specQueue dependency by directly storing actual push address in FTQ.

### FRAS-004
- **Severity**: `Info`
- **Symptom**: nsp/ssp mismatch and commit/spec address mismatch assertions commented out.
- **reason**:
  ```scala
  // RasStack.scala:349,371-373
  // XSError(io.commit.metaSsp =/= nsp, "nsp mismatch with expected ssp")
  // XSError(io.commit.pushAddr =/= commitPushAddr, "addr from commit mismatch with addr from spec")
  ```
- **Improvement suggestion**: Identify cause of annotation processing and reactivate with conditional assertion

### FRAS-005
- **Severity**: `Low`
- **Symptom**: `bos` update condition (`distanceBetween > 2`) occurs unexpectedly, commenting out XSError
- **reason**:
  ```scala
  // RasStack.scala:381-385
  // FIXME: Currently this assertion fails. Fix or reconsider it in the future.
  // XSError(io.commit.valid && (distanceBetween(io.commit.metaTosw, bos) > 2.U), ...)
  ```
- **Improvement Suggestion**: Reexamine the BOS renewal policy and clarify whether it is an intentional condition or a bug.

### FRAS-006
- **Severity**: `High`
- **Symptom**: Neither push nor pop occurs when processing the `PopAndPush (0b11)` (jalr rs1=x1/x5 && rd=x1/x5, rs1≠rd) command.
- **reason**:
  ```scala
  // Ras.scala:65-66
  private val specPush = io.specIn.valid && io.specIn.bits.attribute.isCall      // isCall = Push(0b10) only
  private val specPop  = io.specIn.valid && io.specIn.bits.attribute.isReturn    // isReturn = Pop(0b01) only
// PopAndPush(0b11) isCall=false, isReturn=false → Neither is processed
  ```
- **Improvement suggestion**: Use `hasPush`/`hasPop` bit fields:
  ```scala
  private val specPush = io.specIn.valid && io.specIn.bits.attribute.hasPush
  private val specPop  = io.specIn.valid && io.specIn.bits.attribute.hasPop
  ```

### FRAS-007
- **Severity**: `Medium`
- **Symptom**: Even if there are multiple CFIs such as call+ret in the same fetch block, only the first taken branch is processed.
- **Rationale**: `io.specIn.valid = s3_fire` single valid, S3 prediction expresses only one branch
- **Improvement suggestion**: Introducing multiple event queues that process all calls/rets within a fetch block in order.

---

## 16. Input-to-output latency

| Action | input trigger | Output validity period | latency |
|---|---|---|---|
| push (speculative) | `s3_fire` (Ras.scala) | Next S3 cycle (`timingTop` register) | 1 cycle |
| pop (speculative) | `s3_fire` (Ras.scala) | Next S3 cycle (`timingTop` register) | 1 cycle |
| `topRetAddr` output | Previous S3 push/pop | Current S3 (registered `timingTop`) | 0 (combinational from register) |
| redirect recovery | `io.redirect.valid` | redirect+2 cycle (`RegNextWithEnable` + pointer restoration) | 2 cycle |
| commit processing | `io.commit.valid` | commit+1 cycle (`RegNext` delay) | 1 cycle |

---

## 17. Verification Checklist

### Default behavior
- [ ] call push → check next cycle `topRetAddr == pushAddr`
- [ ] call push → ret pop → confirm restoration of `topRetAddr`
- [ ] Check `specNearOverflowed=true`, push/pop suppression when overflow (SpecQueueSize=32 exceeded)
- [ ] PopAndPush instruction handling (currently unhandled, FRAS-006)

### Redirect recovery
- [ ] Verify checkpoint-pointer restoration accuracy on mispredict redirect
- [ ] Verify prediction behavior during the 2-cycle redirect handling latency
- [ ] Verify the suppression condition of `stack.redirect.valid` during overflow redirect (FRAS-002)
- [ ] Verify redirect `isCall` re-push address equals `cfiPc + 2`

### Commit accuracy
- [ ] Verify address accuracy when commit push uses specQueue slots below 32
- [ ] Verify potential wrong-address issue when commit push occurs after specQueue exceeds slot 32 (FRAS-003)
- [ ] Verify forced-correction behavior on `nsp/ssp` mismatch

### Overflow
- [ ] Verification of overflow state entry/release conditions (`tosw - bos > 30`)
- [ ] Verify whether suppressing redirect during overflow leads to subsequent prediction errors

### writeBypass
- [ ] Check whether `timingTop` accurately reflects the pushed address in the next cycle immediately after push.
- [ ] When redirect+call, writeBypass is updated and next topRetAddr is checked.

---

*Analysis standard files: `src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala`, `RasStack.scala`, `Bundles.scala`, `Parameters.scala`*
*Code commit: `bfbb21862` (branch: `kunminghu-v3`)*

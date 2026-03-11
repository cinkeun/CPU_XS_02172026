# frontend_IBuffer_analysis.md

- Block: Frontend
- Module: IBuffer
- Source: IBuffer.scala, FrontendBundle.scala
- Protocols: Decoupled (in, out × DecodeWidth)
- Key Params: IBufSize=48, IBufNBank=6, PredictWidth=16, DecodeWidth=6
- Last updated: 2026-02-18

→ See [frontend_block_diagram_overview.md](./frontend_block_diagram_overview.md)
→ See [frontend_in_out_seq_diagram.md](./frontend_in_out_seq_diagram.md#group-a)

---

## 1. Module Summary

- **Role**: Buffers fetch packets (up to PredictWidth commands) received from IFU and supplies up to DecodeWidth number of CtrlFlows to the Decode stage every cycle. When IBuffer is full, fetch back-pressures, and when flush, it is fully initialized.
- **Position**: `Frontend.scala` → `Module(new IBuffer)` (inside FrontendInlinedImp)
- **Pipeline stage number**: 1 register stage (Output Reg) + bypass path

---

## 2. Key Parameters

| Parameter | Source | Default | Impact |
| ----------- | ----------------------------------- | ------- | ------------------------------------------ |
| IBufSize | `p(XSCoreParamsKey).IBufSize` | 48 | Total number of buffer entries (IBufNBank × bankSize) |
| IBufNBank | `p(XSCoreParamsKey).IBufNBank` | 6 | Number of banks (requires ≥ DecodeWidth) |
| PredictWidth| `HasXSParameter.PredictWidth` | 16 | Maximum number of enqueue instructions in one cycle |
| DecodeWidth | `p(XSCoreParamsKey).DecodeWidth` | 6 | Maximum number of dequeue instructions in one cycle |
| bankSize | `IBufSize / IBufNBank` | 8 | Number of entries per bank |

Constraints: `IBufSize % IBufNBank == 0`, `IBufNBank >= DecodeWidth`

---

## 3. Interfaces

| Port                     | Dir | Bitwidth          | Protocol  | Description                              |
| ------------------------ | --- | ----------------- | --------- | ---------------------------------------- |
| `io.in` | in | FetchToIBuffer | Decoupled | IFU → IBuffer (PredictWidth commands) |
| `io.out(i)` | out | CtrlFlow | Decoupled | IBuffer → Decode (DecodeWidth) |
| `io.flush` | in | Bool | — | Backend redirect → full flush |
| `io.decodeCanAccept` | in | Bool | — | Decode acceptability |
| `io.full` | out | Bool | — | IBuffer full status (`!allowEnq`) |
| `io.ControlRedirect` | in | Bool | — | flush Cause: Control redirect |
| `io.ControlBTBMissBubble`| in | Bool | — | flush Cause: BTB Miss bubble |
| `io.TAGEMissBubble` | in | Bool | — | flush Cause: TAGE Miss bubble |
| `io.SCMissBubble` | in | Bool | — | flush Cause: SC Miss bubble |
| `io.ITTAGEMissBubble` | in | Bool | — | flush Cause: ITTAGE Miss bubble |
| `io.RASMissBubble` | in | Bool | — | flush Cause: RAS Miss bubble |
| `io.MemVioRedirect` | in | Bool | — | flush Cause: Memory violation redirect |
| `io.stallReason` | out | StallReasonIO | — | Stall cause vector for TopDown analysis |

---

## 4. Internal Pipeline / State

### 4.1 Buffer structure

```
ibuf: Vec[IBufEntry] = RegInit(VecInit.fill(IBufSize)(0))
// IBufSize registers (not SRAM — requires precise R/W control)

bankedIBufView: Vec[Vec[IBufEntry]] =
  VecInit.tabulate(IBufNBank)(bankID =>
    VecInit.tabulate(bankSize)(inBankOffset =>
      ibuf(bankID + inBankOffset * IBufNBank)
    )
  )
// bankID=0: ibuf[0, 6, 12, 18, ...]
// bankID=1: ibuf[1, 7, 13, 19, ...]
// ... interleaving batch
```

### 4.2 Pointer structure

| pointer | Type | role |
| ------- | ---- | ---- |
| `enqPtrVec(i)` | Vec(PredictWidth, IBufPtr) | Each location pointer when enqueued |
| `enqPtr` | IBufPtr | = `enqPtrVec(0)` |
| `deqPtr` | IBufPtr | dequeue position (absolute) |
| `deqBankPtrVec(i)` | Vec(DecodeWidth, IBufBankPtr) | dequeue city bank pointer |
| `deqBankPtr` | IBufBankPtr | = `deqBankPtrVec(0)` |
| `deqInBankPtr(b)` | Vec(IBufNBank, IBufInBankPtr) | Internal pointer for each bank |

Invariant: `deqPtr.value === deqBankPtr.value + deqInBankPtr(deqBankPtr.value).value × IBufNBank`

### 4.3 Output Register (1 stage)

```scala
val outputEntries = RegInit(VecInit.fill(DecodeWidth)(0.U.asTypeOf(Valid(new IBufEntry))))
val outputEntriesValidNum = PriorityMuxDefault(...)
```

- Output register directly connected to Decode
- Complete replacement at `decodeCanAccept`, partial filling at `outputEntriesIsNotFull`

### 4.4 Bypass route

```scala
val useBypass = enqPtr === deqPtr && decodeCanAccept
// Empty state + Decode can be accepted → enqueue immediately passed to output (without going through IBuffer)
```

- `bypassEntries`: Pass data from IFU directly to OutputEntries
- When bypassing, `numTryEnq = max(0, numFromFetch - DecodeWidth)` — enqueue only the rest

---

## 5. Functionality

### 5.1 Enqueue (IFU → IBuffer)

```
numFromFetch = PopCount(io.in.bits.enqEnable) // Actual number of valid instructions
io.in.ready  = allowEnq

// Each PredictWidth command → Calculate the corresponding ibuf index
enqOffset(i) = PopCount(io.in.bits.valid.take(i))
// write at enqPtrVec(enqOffset(i)).value location

// bypass: bypass the first DecodeWidth, enqueue the rest
when(useBypass):
  numBypass  = min(numFromFetch, DecodeWidth)
  numTryEnq  = max(0, numFromFetch - DecodeWidth)
else:
  numBypass  = 0
  numTryEnq  = numFromFetch

when(io.in.fire && !io.flush):
ibuf[enqPtrVec(enqOffset(i) - DecodeWidth + k)].write(enqData(i)) // Excluding bypass
  enqPtrVec += numTryEnq
```

### 5.2 Dequeue (IBuffer → Decode)

```
// 2nd stage read (area optimization)
// Stage 1: Select 1 entry from each bank (bankSize → 1 Mux)
readStage1(bankID) = Mux1H(UIntToOH(deqInBankPtr(bankID).value), bankedIBufView(bankID))

// Stage 2: Select DecodeWidth output (IBufNBank → 1 Mux)
deqEntries(i).bits = Mux1H(UIntToOH(deqBankPtrVec(i).value), readStage1)

// Determine number of outputs
when(decodeCanAccept):
  numOut = min(numValid, DecodeWidth)
when(outputEntriesIsNotFull): // outputEntries last is empty
  numOut = min(numValid, DecodeWidth - outputEntriesValidNum)
else:
  numOut = 0

// update pointer
deqPtr        += numDeq
deqBankPtrVec += numDeq // loop through each bank pointer
deqInBankPtr(b)+= 1  (bankAdvance = numOut > validIdx)
```

### 5.3 Output register update

```
// io.out(i) = outputEntries(i) (Reg)
when(decodeCanAccept):
  if useBypass && io.in.valid:
outputEntries := bypassEntries // bypass bypass direct output
  else:
outputEntries := deqEntries // general dequeue
when(outputEntriesIsNotFull):
// Partial filling: Preserve existing valid entries + add new entries
  outputEntries(i).bits = Mux(i < outputEntriesValidNum,
                              old_out, deqEntries(i - outputEntriesValidNum))
```

### 5.4 Full control

```
numValidNext = numValid + numEnq - numDeq
allowEnq     = (IBufSize - PredictWidth).U >= numValidNext
// Block in advance when almost full (Securing PredictWidth margin)

io.full = !allowEnq → io.frontendInfo.ibufFull (Backend TopDown signal)
```

### 5.5 Flush

```
on io.flush:
  allowEnq      := true.B
enqPtrVec := 0, 1, 2, ... (restore initial value)
deqBankPtrVec := 0, 1, 2, ... (restore initial value)
deqInBankPtr := all 0
  deqPtr        := 0
  outputEntries.foreach(_.valid := false.B)
// maintain ibuf register contents (no valid bit, range managed by pointer)
```

---

## 6. Flow / Backpressure Control

| Conditions | Action |
| ---- | ---- |
| IBuffer full (`!allowEnq`) | `io.in.ready = false` → IFU toIbuffer stall → F3 stall → ICache stall |
| Decode stall (`!decodeCanAccept`) | `numOut = 0` → keep outputEntries → no move deqPtr |
| `outputEntriesIsNotFull` | Decode accepts only part → keeps the rest in output reg |
| Use Bypass | IBuffer empty + decodeCanAccept → enqueue-dequeue simultaneous processing (1-cycle saving) |
| Flush | Immediate full reset, restore `allowEnq := true` |

**back-pressure propagation path:**
```
Backend not accept → decodeCanAccept=false
→ numOut=0 → outputEntries fixed
→ increase numValidNext → allowEnq=false
  → io.in.ready=false → IFU stall → F3 stall
  → icacheStop=true → ICache stall
→ FTQ toIfu.req.ready=false → Block additional IFU requests
```

---

## 7. Error / Exception Handling

IBuffer itself does not generate exceptions, but preserves the exception information received from IFU and passes it to Decode.

| exception information | Save type | Delivery method |
| --------- | --------- | --------- |
| Page Fault (PF) | `IBufferExceptionType.NonCrossPF / CrossPF` | `cf.exceptionVec(instrPageFault) := isPF(...)` |
| Guest Page Fault (GPF) | `NonCrossGPF / CrossGPF` | `cf.exceptionVec(instrGuestPageFault)` |
| Access Fault (AF) | `NonCrossAF / CrossAF` | `cf.exceptionVec(instrAccessFault)` |
| Illegal RVC | `rvcII` | `cf.exceptionVec(EX_II)` |
| Cross-page IPF fix | `CrossPF` | `cf.crossPageIPFFix := isCrossPage(...)` |
| Backend exception | `backendException: Bool` | `cf.backendException` delivered as is |
| Trigger | `TriggerAction()` | `cf.trigger` delivered as is |

`IBufferExceptionType` (3-bit):
```
000 None    001 NonCrossPF   010 NonCrossGPF   011 NonCrossAF
100 rvcII   101 CrossPF      110 CrossGPF      111 CrossAF
bit[2]: isCrossPage, bit[1:0]: exception type
```

### TopDown classification during flush

```
when(io.flush):
topdown_stage.reasons := set based on causes
  BTBMissBubble  ← ControlBTBMissBubble
  TAGEMissBubble ← TAGEMissBubble
  SCMissBubble   ← SCMissBubble
  ITTAGEMissBubble ← ITTAGEMissBubble
  RASMissBubble  ← RASMissBubble
  MemVioRedirectBubble ← MemVioRedirect
  OtherRedirectBubble  ← otherwise

io.stallReason.reason(i) := matchBubble  // for wasted decode slots
```

---

## 8. Timing Hints

| Critical Path | Description |
| ------------- | ---- |
| Enqueue write mux | For each ibuf entry, select 1 of PredictWidth (`Mux1H(validOH, enqData)`) — IBufSize × PredictWidth Mux |
| Read Dequeue Step 2 | Stage1: bankSize→1 Mux × IBufNBank, Stage2: IBufNBank→1 Mux × DecodeWidth |
| `enqOffset` PopCount | `PopCount(io.in.bits.valid.take(i))` × PredictWidth — parallel prefix sum |
| `outputEntriesValidNum` | `PriorityMuxDefault(outputEntries.map(_.valid)...)` — DecodeWidth priority encoder |
| `numValidNext` → `allowEnq` | Comparison after addition — `io.in.ready` critical path |
| deqBankPtr update | `deqBankPtrVec(i) + numDeq` × DecodeWidth — parallel circular ptr addition |

---

## 9. Pseudocode

```
// Enqueue
S0 (combinational):
  numFromFetch = PopCount(in.bits.enqEnable)
  useBypass    = (enqPtr === deqPtr) && decodeCanAccept
  if useBypass:
    numBypass  = min(numFromFetch, DecodeWidth)
    numTryEnq  = max(0, numFromFetch - DecodeWidth)
  else:
    numBypass  = 0
    numTryEnq  = numFromFetch

  enqOffset(i) = PopCount(in.bits.valid.take(i))
  enqData(i)   = IBufEntry.fromFetch(in.bits, i)

  allowEnq = (IBufSize - PredictWidth) >= numValidNext

on in.fire && !flush:
  for each i in PredictWidth:
    if in.bits.valid(i) && in.bits.enqEnable(i) && !bypassed:
      ibuf[enqPtr + enqOffset(i) - numBypass].write(enqData(i))
  enqPtrVec += numTryEnq

// Dequeue
each cycle:
  readStage1(b) = Mux1H(deqInBankPtr(b).value, bankedIBufView(b))
  deqEntries(i) = Mux1H(deqBankPtrVec(i).value, readStage1)

numOut = ... // decodeCanAccept / outputEntriesIsNotFull condition

  if decodeCanAccept:
    if useBypass && in.valid:
      outputEntries := bypassEntries
    else:
      outputEntries := deqEntries
  elif outputEntriesIsNotFull:
    outputEntries(i > outputEntriesValidNum) := deqEntries(i - outputEntriesValidNum)

  deqPtr        += numDeq
  deqBankPtrVec += numDeq
  for b in IBufNBank: if bankAdvance(b): deqInBankPtr(b) += 1

// Output to Decode
io.out(i).valid = outputEntries(i).valid
io.out(i).bits  = outputEntries(i).bits.toCtrlFlow

// Flush
on flush:
  enqPtrVec, deqBankPtrVec, deqInBankPtr, deqPtr := reset
  outputEntries.foreach(_.valid := false)
  allowEnq := true
```

---

## 10. Notes / Assumptions

- `ibuf` is `RegInit` register array (not SRAM) — registers are used for precise write control and bypass
- Bank interleaving (`bankID + inBankOffset × IBufNBank`): When dequeuing, only read a maximum of 1 entry from each bank to save area (IBufNBank:1 Mux + bankSize:1 Mux two steps)
- `allowEnq` Hysteresis: `(IBufSize - PredictWidth).U >= numValidNext` — Worst case (PredictWidth simultaneous enqueue) overflow prevention
- Reason for having 1 output register: Decode timing optimization — Direct reading from `outputEntries` register without combinational
- `IBufNBank >= DecodeWidth` Prerequisite: Each dequeue slot (i) must read a different bank to avoid conflict.
- TopDown `stallReason`: Record the cause in `topdown_stage` for 1 cycle immediately after flush → Tagging the stall cause in an empty slot in Decode
- `headBubble` / `instrHungry`: Distinguish between the state in which the IBuffer is empty after the previous flush (normal empty state vs. delayed fetch)

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

- **역할**: IFU에서 받은 fetch 패킷(최대 PredictWidth개 명령어)을 버퍼링하고, Decode 스테이지로 매 사이클 최대 DecodeWidth개 CtrlFlow를 공급한다. IBuffer full 시 fetch를 back-pressure하고, flush 시 전체 초기화한다.
- **위치**: `Frontend.scala` → `Module(new IBuffer)` (FrontendInlinedImp 내부)
- **Pipeline stage 수**: 1 register stage (Output Reg) + bypass 경로

---

## 2. Key Parameters

| Parameter   | Source                              | Default | 영향                                       |
| ----------- | ----------------------------------- | ------- | ------------------------------------------ |
| IBufSize    | `p(XSCoreParamsKey).IBufSize`       | 48      | 총 버퍼 엔트리 수 (IBufNBank × bankSize)   |
| IBufNBank   | `p(XSCoreParamsKey).IBufNBank`      | 6       | 뱅크 수 (≥ DecodeWidth 필요)              |
| PredictWidth| `HasXSParameter.PredictWidth`       | 16      | 한 사이클 최대 enqueue 명령어 수           |
| DecodeWidth | `p(XSCoreParamsKey).DecodeWidth`    | 6       | 한 사이클 최대 dequeue 명령어 수           |
| bankSize    | `IBufSize / IBufNBank`              | 8       | 뱅크 1개당 엔트리 수                       |

제약: `IBufSize % IBufNBank == 0`, `IBufNBank >= DecodeWidth`

---

## 3. Interfaces

| Port                     | Dir | Bitwidth          | Protocol  | Description                              |
| ------------------------ | --- | ----------------- | --------- | ---------------------------------------- |
| `io.in`                  | in  | FetchToIBuffer    | Decoupled | IFU → IBuffer (PredictWidth개 명령어)    |
| `io.out(i)`              | out | CtrlFlow          | Decoupled | IBuffer → Decode (DecodeWidth개)         |
| `io.flush`               | in  | Bool              | —         | Backend redirect → 전체 flush            |
| `io.decodeCanAccept`     | in  | Bool              | —         | Decode 수락 가능 여부                    |
| `io.full`                | out | Bool              | —         | IBuffer full 상태 (`!allowEnq`)          |
| `io.ControlRedirect`     | in  | Bool              | —         | flush 원인: Control redirect 여부        |
| `io.ControlBTBMissBubble`| in  | Bool              | —         | flush 원인: BTB Miss bubble              |
| `io.TAGEMissBubble`      | in  | Bool              | —         | flush 원인: TAGE Miss bubble             |
| `io.SCMissBubble`        | in  | Bool              | —         | flush 원인: SC Miss bubble               |
| `io.ITTAGEMissBubble`    | in  | Bool              | —         | flush 원인: ITTAGE Miss bubble           |
| `io.RASMissBubble`       | in  | Bool              | —         | flush 원인: RAS Miss bubble              |
| `io.MemVioRedirect`      | in  | Bool              | —         | flush 원인: Memory violation redirect    |
| `io.stallReason`         | out | StallReasonIO     | —         | TopDown 분석용 stall 원인 벡터           |

---

## 4. Internal Pipeline / State

### 4.1 버퍼 구조

```
ibuf: Vec[IBufEntry] = RegInit(VecInit.fill(IBufSize)(0))
// IBufSize개 레지스터 (SRAM 아님 — 정밀한 R/W 제어 필요)

bankedIBufView: Vec[Vec[IBufEntry]] =
  VecInit.tabulate(IBufNBank)(bankID =>
    VecInit.tabulate(bankSize)(inBankOffset =>
      ibuf(bankID + inBankOffset * IBufNBank)
    )
  )
// bankID=0: ibuf[0, 6, 12, 18, ...]
// bankID=1: ibuf[1, 7, 13, 19, ...]
// ... 인터리빙 배치
```

### 4.2 포인터 구조

| 포인터 | 타입 | 역할 |
| ------- | ---- | ---- |
| `enqPtrVec(i)` | Vec(PredictWidth, IBufPtr) | enqueue 시 각 위치 포인터 |
| `enqPtr` | IBufPtr | = `enqPtrVec(0)` |
| `deqPtr` | IBufPtr | dequeue 위치 (절대) |
| `deqBankPtrVec(i)` | Vec(DecodeWidth, IBufBankPtr) | dequeue 시 뱅크 포인터 |
| `deqBankPtr` | IBufBankPtr | = `deqBankPtrVec(0)` |
| `deqInBankPtr(b)` | Vec(IBufNBank, IBufInBankPtr) | 뱅크별 내부 포인터 |

불변식: `deqPtr.value === deqBankPtr.value + deqInBankPtr(deqBankPtr.value).value × IBufNBank`

### 4.3 Output Register (1 stage)

```scala
val outputEntries = RegInit(VecInit.fill(DecodeWidth)(0.U.asTypeOf(Valid(new IBufEntry))))
val outputEntriesValidNum = PriorityMuxDefault(...)
```

- Decode로 직접 연결되는 출력 레지스터
- `decodeCanAccept` 시 전체 교체, `outputEntriesIsNotFull` 시 부분 채움

### 4.4 Bypass 경로

```scala
val useBypass = enqPtr === deqPtr && decodeCanAccept
// 빈 상태 + Decode 수락 가능 → enqueue 즉시 출력으로 전달 (IBuffer 거치지 않음)
```

- `bypassEntries`: IFU에서 온 데이터를 직접 OutputEntries로 전달
- Bypass 시 `numTryEnq = max(0, numFromFetch - DecodeWidth)` — 나머지만 enqueue

---

## 5. Functionality

### 5.1 Enqueue (IFU → IBuffer)

```
numFromFetch = PopCount(io.in.bits.enqEnable)  // 실제 유효 명령어 수
io.in.ready  = allowEnq

// 각 PredictWidth 명령어 → 해당 ibuf 인덱스 계산
enqOffset(i) = PopCount(io.in.bits.valid.take(i))
// enqPtrVec(enqOffset(i)).value 위치에 write

// bypass: 앞 DecodeWidth개는 bypass, 나머지 enqueue
when(useBypass):
  numBypass  = min(numFromFetch, DecodeWidth)
  numTryEnq  = max(0, numFromFetch - DecodeWidth)
else:
  numBypass  = 0
  numTryEnq  = numFromFetch

when(io.in.fire && !io.flush):
  ibuf[enqPtrVec(enqOffset(i) - DecodeWidth + k)].write(enqData(i))  // bypass 제외
  enqPtrVec += numTryEnq
```

### 5.2 Dequeue (IBuffer → Decode)

```
// 2단계 읽기 (면적 최적화)
// Stage 1: 각 뱅크에서 1엔트리 선택 (bankSize → 1 Mux)
readStage1(bankID) = Mux1H(UIntToOH(deqInBankPtr(bankID).value), bankedIBufView(bankID))

// Stage 2: DecodeWidth개 출력 선택 (IBufNBank → 1 Mux)
deqEntries(i).bits = Mux1H(UIntToOH(deqBankPtrVec(i).value), readStage1)

// 출력 수 결정
when(decodeCanAccept):
  numOut = min(numValid, DecodeWidth)
when(outputEntriesIsNotFull):  // outputEntries 마지막이 비어있음
  numOut = min(numValid, DecodeWidth - outputEntriesValidNum)
else:
  numOut = 0

// 포인터 갱신
deqPtr        += numDeq
deqBankPtrVec += numDeq  // 각 뱅크 포인터 순환
deqInBankPtr(b)+= 1  (bankAdvance = numOut > validIdx)
```

### 5.3 Output 레지스터 갱신

```
// io.out(i) = outputEntries(i) (Reg)
when(decodeCanAccept):
  if useBypass && io.in.valid:
    outputEntries := bypassEntries   // bypass 직접 출력
  else:
    outputEntries := deqEntries      // 일반 dequeue
when(outputEntriesIsNotFull):
  // 부분 채움: 기존 valid 엔트리 보존 + 새 엔트리 추가
  outputEntries(i).bits = Mux(i < outputEntriesValidNum,
                              old_out, deqEntries(i - outputEntriesValidNum))
```

### 5.4 Full 제어

```
numValidNext = numValid + numEnq - numDeq
allowEnq     = (IBufSize - PredictWidth).U >= numValidNext
// 거의 full 시 미리 차단 (PredictWidth 마진 확보)

io.full = !allowEnq  → io.frontendInfo.ibufFull (Backend TopDown 신호)
```

### 5.5 Flush

```
on io.flush:
  allowEnq      := true.B
  enqPtrVec     := 0, 1, 2, ... (초기값 복원)
  deqBankPtrVec := 0, 1, 2, ... (초기값 복원)
  deqInBankPtr  := 모두 0
  deqPtr        := 0
  outputEntries.foreach(_.valid := false.B)
// ibuf 레지스터 내용은 유지 (valid 비트 없음, 포인터로 범위 관리)
```

---

## 6. Flow / Backpressure Control

| 조건 | 동작 |
| ---- | ---- |
| IBuffer full (`!allowEnq`) | `io.in.ready = false` → IFU toIbuffer stall → F3 stall → ICache stall |
| Decode stall (`!decodeCanAccept`) | `numOut = 0` → outputEntries 유지 → deqPtr 이동 없음 |
| `outputEntriesIsNotFull` | Decode가 일부만 수락 → 나머지를 output reg에 유지 |
| Bypass 사용 | IBuffer empty + decodeCanAccept → enqueue-dequeue 동시 처리 (1-cycle 절약) |
| Flush | 즉시 전체 초기화, `allowEnq := true` 복원 |

**back-pressure 전파 경로:**
```
Backend not accept → decodeCanAccept=false
  → numOut=0 → outputEntries 고정
  → numValidNext 증가 → allowEnq=false
  → io.in.ready=false → IFU stall → F3 stall
  → icacheStop=true → ICache stall
  → FTQ toIfu.req.ready=false → IFU 추가 요청 차단
```

---

## 7. Error / Exception Handling

IBuffer 자체는 예외를 생성하지 않으며, IFU에서 받은 예외 정보를 보존하여 Decode로 전달한다.

| 예외 정보 | 저장 형태 | 전달 방법 |
| --------- | --------- | --------- |
| Page Fault (PF) | `IBufferExceptionType.NonCrossPF / CrossPF` | `cf.exceptionVec(instrPageFault) := isPF(...)` |
| Guest Page Fault (GPF) | `NonCrossGPF / CrossGPF` | `cf.exceptionVec(instrGuestPageFault)` |
| Access Fault (AF) | `NonCrossAF / CrossAF` | `cf.exceptionVec(instrAccessFault)` |
| Illegal RVC | `rvcII` | `cf.exceptionVec(EX_II)` |
| Cross-page IPF fix | `CrossPF` | `cf.crossPageIPFFix := isCrossPage(...)` |
| Backend exception | `backendException: Bool` | `cf.backendException` 그대로 전달 |
| Trigger | `TriggerAction()` | `cf.trigger` 그대로 전달 |

`IBufferExceptionType` (3-bit):
```
000 None    001 NonCrossPF   010 NonCrossGPF   011 NonCrossAF
100 rvcII   101 CrossPF      110 CrossGPF      111 CrossAF
bit[2]: isCrossPage, bit[1:0]: exception type
```

### Flush 시 TopDown 분류

```
when(io.flush):
  topdown_stage.reasons := 원인에 따라 설정
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

| Critical Path | 설명 |
| ------------- | ---- |
| Enqueue write mux | 각 ibuf 엔트리에 대해 PredictWidth개 중 1개 선택 (`Mux1H(validOH, enqData)`) — IBufSize × PredictWidth Mux |
| Dequeue 2단계 읽기 | Stage1: bankSize→1 Mux × IBufNBank, Stage2: IBufNBank→1 Mux × DecodeWidth |
| `enqOffset` PopCount | `PopCount(io.in.bits.valid.take(i))` × PredictWidth — 병렬 prefix sum |
| `outputEntriesValidNum` | `PriorityMuxDefault(outputEntries.map(_.valid)...)` — DecodeWidth개 우선순위 인코더 |
| `numValidNext` → `allowEnq` | 덧셈 후 비교 — `io.in.ready` critical path |
| deqBankPtr 갱신 | `deqBankPtrVec(i) + numDeq` × DecodeWidth — 병렬 circular ptr 덧셈 |

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

  numOut = ...  // decodeCanAccept / outputEntriesIsNotFull 조건

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

- `ibuf`는 `RegInit` 레지스터 배열 (SRAM 아님) — 정밀한 write 제어와 bypass를 위해 레지스터 사용
- 뱅크 인터리빙 (`bankID + inBankOffset × IBufNBank`): dequeue 시 각 뱅크에서 최대 1엔트리만 읽어 면적 절감 (IBufNBank:1 Mux + bankSize:1 Mux 두 단계)
- `allowEnq` 히스테리시스: `(IBufSize - PredictWidth).U >= numValidNext` — 최악 경우(PredictWidth개 동시 enqueue) 오버플로우 방지
- Output register가 1단 있는 이유: Decode 타이밍 최적화 — `outputEntries` 레지스터에서 combinational 없이 직접 읽기
- `IBufNBank >= DecodeWidth` 필요 조건: 각 dequeue 슬롯(i)이 서로 다른 뱅크를 읽어야 충돌 없음
- TopDown `stallReason`: flush 직후 1 사이클 동안 `topdown_stage`에 원인 기록 → Decode의 빈 슬롯에 stall 원인 태깅
- `headBubble` / `instrHungry`: 직전 flush 후 IBuffer가 비어있는 상태를 구분 (정상적 빈 상태 vs fetch 지연)

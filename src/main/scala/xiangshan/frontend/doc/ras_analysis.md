# RAS (Return Address Stack) 분석 리포트

> **분석 원칙**: code-based only. web-search 및 사전 지식 기반 추론 금지.

---

## 1.1 요약

| 항목 | 내용 |
|---|---|
| 코어/레포/커밋 | XiangShan / `kunminghu-v3` / `bfbb21862` |
| RAS depth | specQueue=32, commitStack=16 |
| ITTAGE 존재 여부 | 있음. 단 `needIttage = isIndirect && !hasPop` 조건. **ret(isReturn=true)에는 RAS 우선, ITTAGE 비사용** |
| 업데이트 방식 | **Hybrid** — speculative push/pop(S3-fire 기반) + commit-time 확정 |
| 복구 방식 | **Checkpoint** — redirect meta(`ssp`, `sctr`, `tosw`, `tosr`, `nos`)를 FTQ에 저장, redirect 시 포인터 일괄 복원 후 재-push/pop |
| 연속 prediction 실패 복구 | redirect마다 checkpoint 복원. 단, `stackNearOverflow=true` 상태에서 redirect 조건 실패 시 복구가 스킵될 수 있음 (RAS-003 참조) |
| Ret 타겟 우선순위 | **RAS 절대 우선**: `isReturn=true`이면 무조건 `ras.io.topRetAddr` 사용. ITTAGE는 `isIndirect && !hasPop` 조건에서만 사용 |
| Multi-fetch 동시 call/ret | **Single-event**: 한 cycle에 하나의 call 또는 ret만 처리 (`io.specIn.valid = s3_fire`, 단일 이벤트) |
| 핵심 리스크 Top 3 | ① overflow 시 redirect 억제 가능성(RAS-003) ② `commitPushAddr = DontCare`(RAS-004) ③ 다중 call/ret 미처리(RAS-005) |

---

## 1.2 관찰 기반 인터페이스 기록

### Ras 모듈 (`Ras.scala`)

| 신호명 | 방향 | 설명 |
|---|---|---|
| `io.specIn.valid` | Input | S3 fire. push/pop 트리거 |
| `io.specIn.bits.attribute.isCall` | Input | push 여부 |
| `io.specIn.bits.attribute.isReturn` | Input | pop 여부 |
| `io.specIn.bits.cfiPosition` | Input | fetch block 내 instruction offset |
| `io.specIn.bits.startPc` | Input | fetch block 시작 PC |
| `io.topRetAddr` | Output | 현재 RAS top (return address). S3에서 사용 |
| `io.redirectMeta` | Output | redirect용 checkpoint meta |
| `io.commitMeta` | Output | commit용 meta |
| `io.redirect.valid` | Input | redirect/recovery 트리거 |
| `io.redirect.bits.attribute.isCall/isReturn` | Input | redirect 시 재-push/pop 여부 |
| `io.redirect.bits.meta.ras` | Input | checkpoint 복원용 meta |
| `io.commit.valid` | Input | commit 확정 |
| `io.commit.bits.attribute.isCall/isReturn` | Input | commit push/pop |

### MicroRas 모듈 (`MicroRas.scala`)

| 신호명 | 방향 | 설명 |
|---|---|---|
| `io.specIn.attribute.isCall/isReturn` | Input | S1의 call/ret 감지 |
| `io.specIn.startPc`, `cfiPosition` | Input | push addr 계산용 |
| `io.hasRedirect` | Input | 전역 redirect 신호 |
| `io.hasOverride` | Input | S3 override 신호 |
| `io.fullRetAddr` | Input | 주 RAS top 주소 (`ras.io.topRetAddr`) |
| `io.specOut.retTarget` | Output | S1에 제공하는 예측 return 주소 |
| `io.specOut.isCanUse` | Output | 예측 유효 여부 |

### RAS 내부 포인터 (`RasStack.scala`)

| 신호명 | 설명 |
|---|---|
| `tosw` | Top of Stack write pointer (specQueue에서 가장 최근 push 위치) |
| `tosr` | Top of Stack read pointer (현재 top 읽기 위치) |
| `ssp` | Committed stack pointer (speculative 참조용) |
| `sctr` | Stack counter (동일 주소 연속 push 압축 카운터, max=7) |
| `nsp` | Non-speculative committed stack pointer |
| `bos` | Bottom of Spec Queue pointer (commit 기준선) |

---

## 1.3 동작 규칙 명문화

### Call 판별 규칙

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/Bundles.scala:52,82-86
def isCall: Bool = rasAction === BranchAttribute.RasAction.Push
// hasPush:
//   branchType === Direct && isLink(rd) && !isRVC  (jal rd=x1/x5, 비압축)
//   branchType === Indirect && isLink(rd)           (jalr rd=x1/x5)
// isLink(reg) = reg === 1 || reg === 5
```

- `jal` with `rd=x1` 또는 `rd=x5` (비압축 명령어, RVC `c.jal`은 RV64에서 `c.addiw`로 decode됨)
- `jalr` with `rd=x1` 또는 `rd=x5`
- `jalr` with `rd=x1/x5` AND `rs1=x1/x5` (PopAndPush: return-and-call)

### Return 판별 규칙

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/Bundles.scala:53,124-125
def isReturn: Bool = rasAction === BranchAttribute.RasAction.Pop
// hasPop:
//   branchType === Indirect && isLink(rs) && rd =/= rs
```

- `jalr` with `rs1=x1` 또는 `rs1=x5`, 단 `rd≠rs1`

### Push 규칙

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:65-74
private val specPush = io.specIn.valid && io.specIn.bits.attribute.isCall
stack.spec.pushValid := specPush && !stackNearOverflow
private val specAlignPc  = specIn.startPc & alignMask      // FetchBlockAlignWidth 기준 정렬
private val specPushAddr = specAlignPc + (specIn.cfiPosition << 1.U).asUInt + 2.U
```

- S3-fire 시 isCall이 참이면 push 발생
- 저장 주소: `(startPc & ~alignMask) + (cfiPosition * 2) + 2` → call 명령어 다음 2바이트 주소 (compressed ISA 기준)
- `stackNearOverflow=true`이면 push 억제

### Pop 규칙

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:66,72
private val specPop = io.specIn.valid && io.specIn.bits.attribute.isReturn
stack.spec.popValid := specPop && !stackNearOverflow
```

- S3-fire 시 isReturn이 참이면 pop 발생
- 반환 주소는 `timingTop.retAddr` (1 cycle 선행 계산된 레지스터)
- `stackNearOverflow=true`이면 pop 억제

### Flush/Redirect 시 복구 규칙

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:93-114
private val redirect = RegNextWithEnable(io.redirect)  // 1사이클 지연

stack.redirect.valid := redirect.valid && (isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow)
// 포인터 일괄 복원
when(io.redirect.valid) {
  tosr := io.redirect.meta.tosr
  tosw := io.redirect.meta.tosw
  ssp  := io.redirect.meta.ssp
  sctr := io.redirect.meta.sctr
  // redirect가 call이면 re-push, ret이면 re-pop
}
```

- redirect 신호는 `RegNextWithEnable`로 1사이클 지연 후 처리
- 저장된 `{ssp, sctr, tosw, tosr, nos}` checkpoint를 복원
- redirect가 call 명령어인 경우: 포인터 복원 후 추가로 specPush 실행
- redirect가 ret 명령어인 경우: 포인터 복원 후 추가로 specPop 실행
- **경고**: `stackNearOverflow=true` 이고 `redirectTOSW >= stackTOSW` 이면 redirect 처리 스킵

### ITTAGE/BTB와 충돌 시 선택 규칙

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/Bpu.scala:355-375
private val s3_useRas    = s3_firstTakenBranch.bits.attribute.isReturn
private val s3_useIttage = s3_firstTakenBranch.bits.attribute.needIttage && ittage.io.prediction.hit
// needIttage = isIndirect && !hasPop

s3_prediction.target := MuxCase(
  s3_fallThroughPrediction.target,
  Seq(
    (s3_taken && s3_useRas)    -> ras.io.topRetAddr,    // 최우선
    (s3_taken && s3_useIttage) -> ittage.io.prediction.target,
    s3_taken                   -> s3_firstTakenBranch.bits.target
  )
)
```

우선순위 (높→낮):
1. `isReturn` → **RAS** (ITTAGE 조건과 상호 배타적: `hasPop`이면 `needIttage=false`)
2. `isIndirect && !hasPop && ittage hit` → **ITTAGE**
3. `taken` → mBTB target
4. fallthrough

### 동일 fetch block 내 다중 call/ret 동시 발생 규칙

- **한 cycle에 단 하나의 이벤트만 처리**: `io.specIn.valid = s3_fire`는 단일 신호
- `s3_prediction`은 fetch block 내 **첫 번째 taken 분기**의 attribute를 사용
- 따라서 같은 fetch block에 call+ret이 있어도, 첫 taken 분기 하나만 처리됨
- `call_call`, `call_ret`, `ret_call`, `ret_ret`: 첫 번째 이벤트만 RAS에 반영

---

## 1.4 이슈 목록

### RAS-001
- **ID**: RAS-001
- **심각도**: `Medium`
- **증상**: `stackNearOverflow=true` 상태에서 push와 pop이 모두 억제되어 RAS 예측 정확도 저하
- **근거**:
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
- **개선 제안**: overflow 시에도 pop은 계속 허용하거나, pop 억제 없이 overflow entry를 순환 덮어쓰기하는 방식 고려

### RAS-002
- **ID**: RAS-002
- **심각도**: `High`
- **증상**: redirect 시 `stackNearOverflow=true` 이고 `!isBefore(redirectTOSW, stackTOSW)` 이면 redirect 처리가 스킵됨
- **근거**:
  ```scala
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:99
  stack.redirect.valid := redirect.valid && (isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow)
  ```
- **개선 제안**: overflow 시에도 redirect를 항상 허용하거나, overflow 상태 진입 시 즉시 RAS reset 고려

### RAS-003
- **ID**: RAS-003
- **심각도**: `Medium`
- **증상**: `commitPushAddr = DontCare` (Ras.scala:108)로 설정되어 실제 push 주소는 `specQueue(metaTosw.value).retAddr`에서 가져오는데, SpecQueueSize(32) 한계 초과 시 specQueue 슬롯이 덮여쓰여질 수 있음
- **근거**:
  ```scala
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala:108
  private val commitPushAddr = DontCare
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/RasStack.scala:352
  private val commitPushAddr = specQueue(io.commit.metaTosw.value).retAddr
  ```
- **개선 제안**: FTQ에 실제 push 주소를 직접 저장하여 specQueue 의존성 제거

### RAS-004
- **ID**: RAS-004
- **심각도**: `Medium`
- **증상**: 다중 call/ret 동시 처리 미지원. 하나의 fetch block에 call+ret이 있으면 첫 번째 이벤트만 처리
- **근거**: `io.specIn.valid = s3_fire` 단일 valid, `s3_prediction`은 하나의 분기만 표현
- **개선 제안**: fetch block 내 모든 call/ret을 순서대로 처리하는 다중 이벤트 큐 도입

### RAS-005
- **ID**: RAS-005
- **심각도**: `Info`
- **증상**: 주석 처리된 `XSError` assertion이 2곳 있음 — nsp-ssp 불일치, commit/spec 주소 불일치
- **근거**:
  ```scala
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/RasStack.scala:349,371-373
  // XSError(io.commit.metaSsp =/= nsp, "nsp mismatch with expected ssp")
  // XSError(io.commit.pushAddr =/= commitPushAddr, "addr from commit mismatch with addr from spec")
  ```
- **개선 제안**: 주석 처리 원인을 파악하고 수정 또는 조건부 assertion으로 재활성화

### RAS-006
- **ID**: RAS-006
- **심각도**: `Low`
- **증상**: `bos` 업데이트의 FIXME 주석 — `distanceBetween(io.commit.metaTosw, bos) > 2` 조건이 예상치 않게 발생
- **근거**:
  ```scala
  // Source: src/main/scala/xiangshan/frontend/bpu/ras/RasStack.scala:381-385
  // FIXME: Currently this assertion fails. Fix or reconsider it in the future.
  // XSError(io.commit.valid && (distanceBetween(io.commit.metaTosw, bos) > 2.U), ...)
  ```
- **개선 제안**: bos 갱신 정책 재검토 및 의도적 조건인지 버그인지 명확화

---

## 1.5 다음 예측 pseudocode

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

## 1.6 Input-to-output latency 및 throughput

| Unit | Input Stage | Output Stage | Latency(cycle) | Throughput(pred/cycle) |
|---|---|---|---|---|
| MicroRas (S1 early) | S1 (`s1_fire`) | S1 (next cycle, registered output) | 1 cycle (prev S1→current S1) | 1 pred/cycle |
| Main RAS push/pop | S3 (`s3_fire`) | S3 (timingTop pre-computed, available next S3) | 0 cycles (within S3 via registered `timingTop`) | 1 pred/cycle |
| Main RAS top read | — | S3 (`ras.io.topRetAddr`) | 0 (combinational from `timingTop` register) | — |
| Redirect recovery | redirect.valid | redirect+2 cycles (RegNextWithEnable + 1) | 2 cycles | — |

**참고**:
- `timingTop`은 레지스터이지만 push/pop 이벤트와 **동일 사이클**에 업데이트됨 (non-blocking assignment)
- 실제로는 "직전 S3 push/pop 이벤트 후 다음 cycle에 topRetAddr이 반영됨"
- MicroRas는 S1에서 결과를 제공하므로 S3보다 2사이클 빠른 early prediction

---

## 1.7 Pipeline stage 위치

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

## 1.8 Training 방법

### Training trigger 정리

| Trigger | Predictor | 입력 | speculative 여부 |
|---|---|---|---|
| S3-fire (speculative) | RAS (push/pop) | `startPc`, `cfiPosition`, `attribute` | Speculative |
| redirect (mispredict) | RAS (restore+redo) | checkpoint meta `{ssp,sctr,tosw,tosr,nos}`, `cfiPc+2` | Recovery |
| commit-valid | commitStack 업데이트 | `attribute.isCall/isReturn`, `metaSsp`, `metaTosw` | Non-speculative |

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

#### 1.8.1 training 트리거 상세

**resolve/mispredict 기반**:
- `io.redirect` (BpuRedirect) 수신 시 RAS 복구
- `redirect.bits.meta.ras` 필드로 checkpoint 복원
- redirect.isCall이면 re-push, redirect.isRet이면 re-pop
- RAS 자체는 별도 테이블 학습 없음 — 순수 stack 복원

**commit 기반**:
- `io.commit.valid`(RegNext 지연 1사이클) 수신 시 commitStack 업데이트
- push: `specQueue(metaTosw.value).retAddr`에서 주소 읽어 commitStack에 씀
- pop: commitStack[nsp].ctr 감소, nsp 조정

**fast-train (S3 override 기반)**:
- MicroRas는 S3 override 신호를 받아 S1-S2 in-flight 상태를 즉시 리셋

#### 1.8.2 FTQ 저장 정보

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/Bundles.scala:261-275
class BpuRedirectMeta(implicit p: Parameters) extends BpuBundle {
  val ras: RasRedirectMeta = new RasRedirectMeta  // ssp, sctr, tosw, tosr, nos, topRetAddr
}
class BpuCommitMeta(implicit p: Parameters) extends BpuBundle {
  val ras: RasCommitMeta = new RasCommitMeta      // ssp, tosw
}
```

| 저장 정보 | Field | 용도 |
|---|---|---|
| `ssp` (speculative stack ptr) | redirectMeta.ras.ssp | redirect 시 복원 |
| `sctr` (stack counter) | redirectMeta.ras.sctr | redirect 시 복원 |
| `tosw` (write ptr) | redirectMeta.ras.tosw, commitMeta.ras.tosw | redirect/commit 시 사용 |
| `tosr` (read ptr) | redirectMeta.ras.tosr | redirect 시 복원 |
| `nos` (next-of-stack ptr) | redirectMeta.ras.nos | redirect pop 시 다음 top 계산 |
| `topRetAddr` | redirectMeta.ras.topRetAddr | (저장은 하나 실제 사용 여부 미확인) |
| `ssp` (commit) | commitMeta.ras.ssp | commit 시 nsp 보정 |
| `tosw` (commit) | commitMeta.ras.tosw | commitStack 주소 조회용 |

#### 1.8.3 FTQ 내 저장 위치

- `BpuMeta.redirectMeta.ras` (RasRedirectMeta): **FTQ entry field 직접 저장**
- `BpuMeta.commitMeta.ras` (RasCommitMeta): **FTQ entry field 직접 저장**
- 별도 meta RAM 없음 — FTQ entry 내 inline 저장

#### 1.8.4 write port congestion 처리

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/RasStack.scala:305-313
realPush := RegNext(io.spec.pushValid, init = false.B) || RegNext(...)
when(realPush) {
  specQueue(realWriteAddr.value) := realWriteEntry
  specNos(realWriteAddr.value)   := realNos
}
```

- specQueue 실제 쓰기(`realPush`)는 push 이벤트로부터 **1 cycle 지연**
- 지연 동안 `writeBypassEntry/writeBypassValid`로 bypass
- 포인터(tosw/tosr/ssp/sctr) 업데이트는 즉각적(같은 cycle)
- **single write port**: arbiter 없음, 한 cycle에 하나의 push 이벤트만 처리

---

## 2) Memory 구조 분석

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

### 2.2 Memory update/recovery 경로

**Speculative push 경로**:
1. S3-fire 시 `io.spec.pushValid` 활성화
2. 포인터(tosw/ssp/sctr) 즉각 업데이트
3. `writeBypassEntry` 즉각 업데이트 (다음 cycle 읽기용 bypass)
4. `realPush = RegNext(pushValid)` → 다음 cycle에 specQueue 실제 쓰기

**Speculative pop 경로**:
1. S3-fire 시 `io.spec.popValid` 활성화
2. `topNos`를 참조해 `tosr` 업데이트
3. `timingTop` 레지스터: 다음 top을 미리 계산하여 저장

**Flush/redirect 복구 경로**:
1. `redirect.valid` 수신 → `RegNextWithEnable`로 1cycle 지연
2. `stack.redirect.valid` 조건 평가
3. `tosr/tosw/ssp/sctr` 일괄 복원
4. isCall이면 push, isRet이면 pop 추가 실행
5. `writeBypass` 갱신

**Commit 확정 경로**:
1. `io.commit.valid` → `RegNext`로 1cycle 지연
2. pushValid이면: specQueue[metaTosw]에서 주소 읽어 commitStack[nsp] 업데이트, nsp 전진
3. popValid이면: commitStack[nsp].ctr 감소, nsp 후진
4. `bos` 업데이트: commit push 시 `bos := metaTosw`

---

## 3) RAS Entry 정의

### 6.1 Entry 정의 코드 snippet

```scala
// Source: src/main/scala/xiangshan/frontend/bpu/ras/Bundles.scala:27-39
class RasEntry(implicit p: Parameters) extends RasBundle {
  val retAddr: PrunedAddr = PrunedAddr(VAddrBits)
  val ctr:     UInt       = UInt(StackCounterWidth.W)  // StackCounterWidth=3
}
```

### 6.2 Entry field 설명

| Field Name | Width(bit) | Description |
|---|---|---|
| `retAddr` | VAddrBits (pruned) | 반환 주소. `PrunedAddr`로 상위 비트 일부 pruning |
| `ctr` | 3 | 동일 반환 주소의 연속 push 압축 카운터. 0 = 1번, 7 = 8번(max) |

**압축 동작**: 같은 `retAddr`가 연속으로 push될 때 ctr을 증가시키고 새 슬롯 할당 없이 기존 슬롯에 누적. pop 시 ctr > 0이면 ctr만 감소, ctr = 0이면 실제 슬롯 해제.

---

## 4) 다중 call/ret 동시 처리 규칙

| 케이스 | 처리 방식 | 결과 |
|---|---|---|
| `call_call` (같은 fetch block) | 첫 taken 분기만 처리 | 두 번째 call 미처리 → 복귀 주소 스택 불완전 |
| `call_ret` | call이 첫 taken이면 call만 처리 | ret 미처리 |
| `ret_call` | ret이 첫 taken이면 ret만 처리 | call 미처리 |
| `ret_ret` | 첫 ret만 처리 | 두 번째 ret 미처리 |
| 동일 slot call+ret (PopAndPush) | `isReturnAndCall = rasAction === PopAndPush` | **Ras.scala에서는 isCall/isReturn만 체크** — PopAndPush가 정상 처리되는지 코드에서 명확히 확인 필요 |

**주목**: `BranchAttribute.PopAndPush` (`RasAction.PopAndPush = 0b11`) 존재하나, `Ras.scala`의 조건은:
```scala
private val specPush = io.specIn.valid && io.specIn.bits.attribute.isCall
private val specPop  = io.specIn.valid && io.specIn.bits.attribute.isReturn
// isCall = rasAction === Push (0b10)
// isReturn = rasAction === Pop (0b01)
// PopAndPush (0b11)이면 isCall=false, isReturn=false → 둘 다 처리 안 됨!
```

**RAS-007** (Critical):
- **심각도**: `High`
- **증상**: `PopAndPush (0b11)` (return-and-call: jalr rs1=x1/x5, rd=x1/x5, rs1≠rd) 명령어 처리 시 push도 pop도 발생하지 않음
- **근거**:
  ```scala
  // Bundles.scala:54: def isReturnAndCall: Bool = rasAction === RasAction.PopAndPush
  // Ras.scala:65-66:
  private val specPush = io.specIn.valid && io.specIn.bits.attribute.isCall      // isCall = Push(0b10) only
  private val specPop  = io.specIn.valid && io.specIn.bits.attribute.isReturn    // isReturn = Pop(0b01) only
  // PopAndPush(0b11) is neither
  ```
- **개선 제안**: `hasPush/hasPop` 비트 필드를 사용하도록 변경:
  ```scala
  private val specPush = io.specIn.valid && io.specIn.bits.attribute.hasPush
  private val specPop  = io.specIn.valid && io.specIn.bits.attribute.hasPop
  ```

---

## 5) 검증(테스트) 기준

### 기본 케이스

- [x] 기본 call/ret — call push 후 ret pop
- [x] 중첩 call/ret — push 2회 후 pop 2회
- [ ] overflow 경계 — SpecQueueSize(32) 초과 call 시 specNearOverflow=true, push/pop 억제
- [ ] underflow — pop이 push보다 많을 때 commitStack fallback 동작
- [ ] redirect 인접 cycle ret — redirect 직후 cycle의 ret가 올바른 주소를 사용하는지
- [ ] PopAndPush (return-and-call) 명령어 처리 (현재 미처리 확인 필요)

### 다중 이벤트 케이스

| 케이스 | 기대 동작 | 검증 포인트 |
|---|---|---|
| `call_call` 동일 블록 | 첫 call만 push | 두 번째 call ret addr 누락 확인 |
| `call_ret` 동일 블록 | call만 처리 (첫 taken 기준) | S3에서 어떤 분기가 selected되는지 |
| `ret_ret` 동일 블록 | 첫 ret만 pop | 두 번째 ret mispredict 발생률 |
| `ret_call` 동일 블록 | ret만 처리 | 동일 |

### RAS/ITTAGE 우선순위 케이스

- [ ] `isReturn=true` 분기 예측 시 RAS만 사용, ITTAGE 무시 확인
- [ ] `isIndirect && !hasPop` 분기에서 ITTAGE hit 시 ITTAGE 사용 확인

### Overflow/Recovery 케이스

- [ ] overflow 중 redirect → `stack.redirect.valid` 억제 조건 검증
- [ ] MicroRas redirect 직후 1 cycle 동안 `isCanUse=false` 확인 (`redirectDelay1`)

### Commit port 케이스

- [ ] 연속 commit push → specQueue slot 재사용 시 주소 정확성 확인
- [ ] commitStack 단일 write port — 동시 push/pop 없음 확인 (둘 중 하나만 활성화)

---

## 6) 기능 체크리스트

- [x] Call/Ret 판별 정확성 — RISC-V spec Table 3 기반 구현 확인
- [x] Push 주소 계산 — `alignedPC + (cfiPosition<<1) + 2` (compressed ISA 2바이트 기준)
- [x] Speculative update — S3-fire 기반 즉각 포인터 업데이트
- [x] Redirect rollback — checkpoint 기반 포인터 복원 + re-push/pop
- [~] Overflow 처리 — specNearOverflow 시 push/pop 억제 (redirect 억제 문제 존재)
- [x] ITTAGE 동시 hit 우선순위 — RAS > ITTAGE 명확히 구현됨
- [x] Commit stack 분리 유지
- [!] PopAndPush (return-and-call) — 현재 처리되지 않음 (RAS-007)
- [!] 다중 call/ret 동시 처리 — 미지원 (단일 이벤트만)
- [x] 연속 prediction 실패 복구 — redirect마다 checkpoint로 복원 (overflow 예외 주의)

---

*분석 기준 파일: `src/main/scala/xiangshan/frontend/bpu/ras/` 전체 + `Bpu.scala` + `Bundles.scala`*
*코드 커밋: `bfbb21862` (branch: `kunminghu-v3`)*

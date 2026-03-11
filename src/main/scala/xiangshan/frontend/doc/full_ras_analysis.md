# Full RAS (Ras + RasStack) 분석 리포트

> **분석 원칙**: code-based only. web-search 및 사전 지식 기반 추론 금지.
> **분석 대상**: `src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala` + `RasStack.scala` + `Bundles.scala` + `Parameters.scala`
> **코어/레포/커밋**: XiangShan / `kunminghu-v3` / `bfbb21862`

---

## 1. 구조 개요

Full RAS는 두 모듈로 구성:

| 모듈 | 파일 | 역할 |
|---|---|---|
| `Ras` | `Ras.scala` | 상위 래퍼. BPU 파이프라인과 인터페이스. push/pop/redirect/commit 라우팅 |
| `RasStack` | `RasStack.scala` | 실제 메모리(specQueue, commitStack)와 포인터 관리 로직 |

동작 파이프라인 위치: **S3** (`io.specIn.valid = s3_fire` 기반)

---

## 2. 파라미터

```scala
// Source: Parameters.scala:21-27
case class RasParameters(
    CommitStackSize:   Int = 16,   // commitStack 깊이
    SpecQueueSize:     Int = 32,   // specQueue 깊이 (반드시 pow2)
    StackCounterWidth: Int = 3     // ctr 비트 폭 → max counter = 7
)
```

| 파라미터 | 값 | 설명 |
|---|---|---|
| `CommitStackSize` | 16 | commit된 call/ret 기록 스택 깊이 |
| `SpecQueueSize` | 32 | speculative push 엔트리 큐 깊이 |
| `StackCounterWidth` | 3 | 동일 주소 연속 push 압축 카운터 폭 (max=7) |

---

## 3. 메모리 구조

### 3.1 Entry 정의

```scala
// Source: Bundles.scala:27-30
class RasEntry(implicit p: Parameters) extends RasBundle {
  val retAddr: PrunedAddr = PrunedAddr(VAddrBits)  // 반환 주소 (상위 비트 pruning)
  val ctr:     UInt       = UInt(StackCounterWidth.W)  // 연속 push 압축 카운터
}
```

**ctr 압축 동작**:
- 같은 `retAddr`가 연속 push될 때 새 슬롯 없이 `ctr++`
- pop 시 `ctr > 0`이면 `ctr--`만, `ctr == 0`이면 슬롯 해제

### 3.2 메모리 테이블

| 메모리 | 타입 | Depth | Width | Read port | Write port |
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

## 4. 포인터 정의

```scala
// Source: RasStack.scala:71-78
private val nsp  = RegInit(0.U(log2Up(CommitStackSize).W))  // commit stack pointer
private val ssp  = RegInit(0.U(log2Up(CommitStackSize).W))  // speculative committed stack pointer
private val sctr = RegInit(0.U(StackCounterWidth.W))         // speculative counter
private val tosr = RegInit(RasPtr(true.B, (SpecQueueSize-1).U)) // spec queue read ptr
private val tosw = RegInit(RasPtr(false.B, 0.U))             // spec queue write ptr
private val bos  = RegInit(RasPtr(false.B, 0.U))             // bottom of spec queue (commit 기준선)
```

| 포인터 | 설명 |
|---|---|
| `nsp` | commit stack의 현재 top 포인터 (non-speculative) |
| `ssp` | commit stack의 speculative 참조 포인터 |
| `sctr` | 동일 주소 연속 push 카운터 (speculative) |
| `tosw` | specQueue write pointer (최신 push 위치의 다음) |
| `tosr` | specQueue read pointer (현재 top 읽기 위치) |
| `bos` | specQueue에서 commit이 확인된 하한선 (Bottom of Spec) |

---

## 5. Ras 모듈 인터페이스 (상위)

```scala
// Source: Ras.scala:44-51
val specIn:   Valid[RasSpecInfo] = Flipped(Valid(new RasSpecInfo))
val commit:   Valid[BpuCommit]   = Flipped(Valid(new BpuCommit))
val redirect: Valid[BpuRedirect] = Flipped(Valid(new BpuRedirect))
val topRetAddr:   PrunedAddr      = Output(PrunedAddr(VAddrBits))
val redirectMeta: RasRedirectMeta = Output(new RasRedirectMeta)
val commitMeta:   RasCommitMeta   = Output(new RasCommitMeta)
```

| 신호명 | 방향 | 타이밍 | 설명 |
|---|---|---|---|
| `specIn.valid` | Input | `s3_fire` | push/pop 트리거 |
| `specIn.bits.attribute.isCall` | Input | S3 | push 여부 |
| `specIn.bits.attribute.isReturn` | Input | S3 | pop 여부 |
| `specIn.bits.cfiPosition` | Input | S3 | fetch block 내 CFI offset |
| `specIn.bits.startPc` | Input | S3 | fetch block 시작 PC |
| `topRetAddr` | Output | S3 | 현재 top return 주소 (`timingTop.retAddr`) |
| `redirectMeta` | Output | S3 | FTQ 저장용 checkpoint (`ssp,sctr,tosw,tosr,nos,topRetAddr`) |
| `commitMeta` | Output | S3 | FTQ 저장용 commit meta (`ssp,tosw`) |
| `redirect.valid` | Input | — | mispredict 복구 트리거 |
| `redirect.bits.meta.ras` | Input | — | 복원할 checkpoint |
| `commit.valid` | Input | — | commit 확정 |

---

## 6. Push 동작 (Speculative)

```scala
// Source: Ras.scala:59-74
def alignMask: UInt = ((~0.U(VAddrBits.W)) << FetchBlockAlignWidth).asUInt
private val specPush     = io.specIn.valid && io.specIn.bits.attribute.isCall
private val specAlignPc  = specIn.startPc & alignMask
private val specPushAddr = specAlignPc + (specIn.cfiPosition << 1.U).asUInt + 2.U
stack.spec.pushValid := specPush && !stackNearOverflow
```

**주소 계산**: `(startPc & alignMask) + (cfiPosition * 2) + 2`
- `alignMask`: `FetchBlockAlignWidth` 비트 단위 정렬 마스크
- `cfiPosition * 2`: 2바이트 단위 offset (RVC 기준)
- `+ 2`: CALL 명령어 다음 주소 (2바이트 단위)

**억제 조건**: `stackNearOverflow = true`이면 push 차단

### RasStack.specPush 내부

```scala
// Source: RasStack.scala:132-149
def specPush(retAddr, currSsp, currSctr, currTosr, currTosw, topEntry): Unit = {
  tosr := currTosw
  tosw := specPtrInc(currTosw)
  when(topEntry.retAddr === retAddr && currSctr < StackCounterMax.U) {
    sctr := currSctr + 1.U   // 동일 주소: ctr만 증가
  }.otherwise {
    ssp  := ptrInc(currSsp)
    sctr := 0.U               // 새 주소: ssp 전진 + ctr reset
  }
}
```

**실제 specQueue 쓰기**:
```scala
// Source: RasStack.scala:305-313
realPush := RegNext(io.spec.pushValid, init = false.B) || RegNext(io.redirect.valid && io.redirect.isCall, ...)
when(realPush) {
  specQueue(realWriteAddr.value) := realWriteEntry
  specNos(realWriteAddr.value)   := realNos
}
```
- 포인터 업데이트: **즉각** (push 이벤트 cycle에)
- specQueue 메모리 쓰기: **1 cycle 지연** (`RegNext`)
- 지연 기간: `writeBypassEntry/writeBypassValid`로 bypass

---

## 7. Pop 동작 (Speculative)

```scala
// Source: Ras.scala:66,72
private val specPop = io.specIn.valid && io.specIn.bits.attribute.isReturn
stack.spec.popValid := specPop && !stackNearOverflow
```

**억제 조건**: `stackNearOverflow = true`이면 pop 차단

### RasStack.specPop 내부

```scala
// Source: RasStack.scala:151-169
def specPop(currSsp, currSctr, currTosr, currTosw, currTopNos): Unit = {
  when(tosrInRange(currTosr, currTosw)) {
    tosr := currTopNos   // specQueue 내 엔트리 → NOS로 이동
  }
  when(currSctr > 0.U) {
    sctr := currSctr - 1.U     // 카운터만 감소
  }.elsewhen(tosrInRange(currTopNos, currTosw)) {
    ssp  := ptrDec(currSsp)
    sctr := specQueue(currTopNos.value).ctr   // in-flight 데이터 사용
  }.otherwise {
    ssp  := ptrDec(currSsp)
    sctr := getCommitTop(ptrDec(currSsp)).ctr  // commit 데이터 fallback
  }
}
```

---

## 8. timingTop: 출력 주소 계산

```scala
// Source: RasStack.scala:220-287
private val timingTop = RegInit(0.U.asTypeOf(new RasEntry))
```

`timingTop`은 **다음 cycle에 필요한 top을 미리 계산**하는 레지스터.
매 cycle 아래 우선순위로 업데이트:

| 조건 | `timingTop` 업데이트 값 |
|---|---|
| `writeBypassValidWire && (redirect.isCall || spec.pushValid)` | `writeEntry` (방금 push된 엔트리) |
| `writeBypassValidWire` (bypass only) | `writeBypassEntry` |
| `redirect.valid && redirect.isRet` | redirect 복원 후 NOS 기반 top 계산 |
| `redirect.valid` (non-call/ret) | redirect meta 기반 top |
| `spec.popValid` | NOS 기반 다음 top 계산 |
| `realPush` | `realWriteEntry` |
| otherwise | 현재 포인터 기반 `getTop()` |

```scala
// Source: RasStack.scala:323
io.spec.popAddr := timingTop.retAddr
// Ras.scala:91
io.topRetAddr := stack.spec.popAddr
```

---

## 9. Redirect/복구 동작

```scala
// Source: Ras.scala:93-104
private val redirect = RegNextWithEnable(io.redirect)  // 1 cycle 지연
stack.redirect.valid := redirect.valid && (isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow)
stack.redirect.meta  := redirect.bits.meta.ras
stack.redirect.callAddr := redirect.bits.cfiPc + 2.U
```

**1. redirect 신호 지연**: `RegNextWithEnable` → 1 cycle 후 RasStack에 전달
**2. 처리 조건**: `isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow`
   - overflow 상태이고 redirectTOSW >= stackTOSW이면 **처리 스킵** → 이슈 RAS-002

```scala
// Source: RasStack.scala:387-412
when(io.redirect.valid) {
  tosr := io.redirect.meta.tosr    // 포인터 일괄 복원
  tosw := io.redirect.meta.tosw
  ssp  := io.redirect.meta.ssp
  sctr := io.redirect.meta.sctr

  when(io.redirect.isCall) { specPush(...) }  // re-push
  when(io.redirect.isRet)  { specPop(...)  }  // re-pop
}
```

**복구 순서**:
1. checkpoint 포인터 복원 (`tosr, tosw, ssp, sctr`)
2. redirect가 CALL이면: re-push (`callAddr = cfiPc + 2`)
3. redirect가 RET이면: re-pop
4. `writeBypass` 갱신

---

## 10. Commit 동작

```scala
// Source: Ras.scala:106-114
private val commitValid = RegNext(io.commit.valid, init = false.B)  // 1 cycle 지연
private val commitInfo  = RegEnable(io.commit.bits, io.commit.valid)
stack.commit.valid     := commitValid
stack.commit.pushValid := commitValid && commitInfo.attribute.isCall
stack.commit.popValid  := commitValid && commitInfo.attribute.isReturn
stack.commit.pushAddr  := DontCare  // ← 실제 주소는 RasStack 내부에서 specQueue 조회
stack.commit.metaTosw  := commitInfo.meta.ras.tosw
stack.commit.metaSsp   := commitInfo.meta.ras.ssp
```

**commit push 경로** (RasStack.scala:352-374):
```scala
private val commitPushAddr = specQueue(io.commit.metaTosw.value).retAddr  // specQueue에서 주소 조회
when(io.commit.pushValid) {
  when(commitTop.ctr < StackCounterMax.U && commitTop.retAddr === commitPushAddr) {
    commitStack(nspUpdate).ctr := commitTop.ctr + 1.U  // ctr 압축
  }.otherwise {
    nsp := ptrInc(nspUpdate)
    commitStack(ptrInc(nspUpdate)).retAddr := commitPushAddr
    commitStack(ptrInc(nspUpdate)).ctr     := 0.U
  }
}
```

**commit pop 경로** (RasStack.scala:333-350):
```scala
when(io.commit.popValid) {
  when(commitTop.ctr > 0.U) {
    commitStack(nspUpdate).ctr := commitTop.ctr - 1.U
  }.otherwise {
    nsp := ptrDec(nspUpdate)
  }
}
```

**nsp 보정**:
- `io.commit.metaSsp =/= nsp` 시 강제로 `nsp := metaSsp` (오류 누적 방지)
- XSError assertion은 주석 처리됨 (이슈 FRAS-003)

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

- `tosw`와 `bos` 간 거리가 `SpecQueueSize - 2 = 30`을 초과하면 overflow 상태
- **push 차단 + pop 차단** 동시 발생 (Ras.scala:71-72)
- redirect 억제 가능성 (Ras.scala:99)

---

## 12. bos 업데이트

```scala
// Source: RasStack.scala:376-380
when(io.commit.pushValid) {
  bos := io.commit.metaTosw
}.elsewhen(io.commit.valid && (distanceBetween(io.commit.metaTosw, bos) > 2.U)) {
  bos := specPtrDec(io.commit.metaTosw)
}
```

- commit push 시: `bos := metaTosw`
- commit valid이지만 push는 아닌데 `distanceBetween > 2`이면: `bos := metaTosw - 1`
- FIXME 주석 처리된 XSError 있음 (이슈 FRAS-005)

---

## 13. Checkpoint (FTQ 저장 정보)

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
  val topRetAddr: PrunedAddr  // 현재 top return 주소 (= timingTop at S3)
}
```

| 필드 | 용도 |
|---|---|
| `ssp` | redirect 시 committed stack pointer 복원 |
| `sctr` | redirect 시 카운터 복원 |
| `tosw` | redirect 시 write pointer 복원; commit 시 specQueue 주소 조회 |
| `tosr` | redirect 시 read pointer 복원 |
| `nos` | redirect pop 시 다음 top 계산 |
| `topRetAddr` | 저장되나 사용 여부 미확인 |

### commitMeta (RasCommitMeta)

```scala
// Source: Bundles.scala:80-83
class RasCommitMeta {
  val ssp:  UInt   // commit 시 nsp 보정용
  val tosw: RasPtr // specQueue 주소 조회용
}
```

---

## 14. writeBypass 메커니즘

specQueue 쓰기는 1 cycle 지연되므로, push 직후 다음 cycle 읽기를 위한 bypass가 필요:

```scala
// Source: RasStack.scala:81-85
private val writeBypassEntry = Reg(new RasEntry)
private val writeBypassNos   = Reg(new RasPtr)
private val writeBypassValid = RegInit(0.B)
```

**활성화/비활성화**:
- push 또는 redirect+call → `writeBypassValid = true`, `writeBypassEntry = writeEntry`
- redirect (non-call) → `writeBypassValid = false` (클리어)
- `spec.fire`이지만 push가 아니면 → `writeBypassValid = false`

**사용**:
- `getTop()`: `allowBypass=true`이면 `writeBypassValid` 시 bypass 우선 사용
- `timingTop` 계산에서 `writeBypassValidWire` 기반 최우선 처리

---

## 15. 이슈 목록

### FRAS-001
- **심각도**: `Medium`
- **증상**: `stackNearOverflow=true` 시 push와 pop이 모두 억제됨
- **근거**:
  ```scala
  // Ras.scala:71-72
  stack.spec.pushValid := specPush && !stackNearOverflow
  stack.spec.popValid  := specPop && !stackNearOverflow
  ```
- **개선 제안**: overflow 시에도 pop은 허용하거나 순환 덮어쓰기 방식 고려

### FRAS-002
- **심각도**: `High`
- **증상**: `stackNearOverflow=true` + `redirectTOSW >= stackTOSW` → redirect 처리 스킵
- **근거**:
  ```scala
  // Ras.scala:99
  stack.redirect.valid := redirect.valid && (isBefore(redirectTOSW, stackTOSW) || !stackNearOverflow)
  ```
- **개선 제안**: overflow 시에도 redirect는 항상 허용하거나, overflow 진입 시 즉시 RAS reset 고려

### FRAS-003
- **심각도**: `Medium`
- **증상**: commit push 주소가 `DontCare`로 전달되어 RasStack이 specQueue에서 직접 조회. specQueue 슬롯이 32를 초과하여 덮어쓰이면 잘못된 주소 commit 가능
- **근거**:
  ```scala
  // Ras.scala:108
  private val commitPushAddr = DontCare
  // RasStack.scala:352
  private val commitPushAddr = specQueue(io.commit.metaTosw.value).retAddr
  ```
- **개선 제안**: FTQ에 실제 push 주소를 직접 저장하여 specQueue 의존성 제거

### FRAS-004
- **심각도**: `Info`
- **증상**: nsp/ssp 불일치 및 commit/spec 주소 불일치 assertion 주석 처리됨
- **근거**:
  ```scala
  // RasStack.scala:349,371-373
  // XSError(io.commit.metaSsp =/= nsp, "nsp mismatch with expected ssp")
  // XSError(io.commit.pushAddr =/= commitPushAddr, "addr from commit mismatch with addr from spec")
  ```
- **개선 제안**: 주석 처리 원인 파악 및 조건부 assertion으로 재활성화

### FRAS-005
- **심각도**: `Low`
- **증상**: `bos` 업데이트 조건(`distanceBetween > 2`)이 예상치 않게 발생하여 XSError 주석 처리
- **근거**:
  ```scala
  // RasStack.scala:381-385
  // FIXME: Currently this assertion fails. Fix or reconsider it in the future.
  // XSError(io.commit.valid && (distanceBetween(io.commit.metaTosw, bos) > 2.U), ...)
  ```
- **개선 제안**: bos 갱신 정책 재검토 및 의도적 조건인지 버그인지 명확화

### FRAS-006
- **심각도**: `High`
- **증상**: `PopAndPush (0b11)` (jalr rs1=x1/x5 && rd=x1/x5, rs1≠rd) 명령어 처리 시 push도 pop도 미발생
- **근거**:
  ```scala
  // Ras.scala:65-66
  private val specPush = io.specIn.valid && io.specIn.bits.attribute.isCall      // isCall = Push(0b10) only
  private val specPop  = io.specIn.valid && io.specIn.bits.attribute.isReturn    // isReturn = Pop(0b01) only
  // PopAndPush(0b11)은 isCall=false, isReturn=false → 둘 다 처리 안 됨
  ```
- **개선 제안**: `hasPush`/`hasPop` 비트 필드 사용:
  ```scala
  private val specPush = io.specIn.valid && io.specIn.bits.attribute.hasPush
  private val specPop  = io.specIn.valid && io.specIn.bits.attribute.hasPop
  ```

### FRAS-007
- **심각도**: `Medium`
- **증상**: 동일 fetch block에 call+ret 등 다중 CFI가 있어도 첫 번째 taken 분기 하나만 처리
- **근거**: `io.specIn.valid = s3_fire` 단일 valid, S3 prediction은 하나의 분기만 표현
- **개선 제안**: fetch block 내 모든 call/ret을 순서대로 처리하는 다중 이벤트 큐 도입

---

## 16. Input-to-output latency

| 동작 | 입력 트리거 | 출력 유효 시점 | latency |
|---|---|---|---|
| push (speculative) | `s3_fire` (Ras.scala) | 다음 S3 cycle (`timingTop` 레지스터) | 1 cycle |
| pop (speculative) | `s3_fire` (Ras.scala) | 다음 S3 cycle (`timingTop` 레지스터) | 1 cycle |
| `topRetAddr` 출력 | 이전 S3 push/pop | 현재 S3 (registered `timingTop`) | 0 (combinational from register) |
| redirect 복구 | `io.redirect.valid` | redirect+2 cycle (`RegNextWithEnable` + 포인터 복원) | 2 cycle |
| commit 처리 | `io.commit.valid` | commit+1 cycle (`RegNext` 지연) | 1 cycle |

---

## 17. 검증 체크리스트

### 기본 동작
- [ ] call push → 다음 cycle `topRetAddr == pushAddr` 확인
- [ ] call push → ret pop → `topRetAddr` 복원 확인
- [ ] overflow(SpecQueueSize=32 초과) 시 `specNearOverflowed=true`, push/pop 억제 확인
- [ ] PopAndPush 명령어 처리 (현재 미처리, FRAS-006)

### Redirect 복구
- [ ] mispredict redirect 시 checkpoint 포인터 복원 정확성 확인
- [ ] redirect 처리 2-cycle latency 동안 예측 결과 확인
- [ ] overflow 중 redirect → `stack.redirect.valid` 억제 조건 검증 (FRAS-002)
- [ ] redirect isCall → re-push 주소 = `cfiPc + 2` 확인

### Commit 정확성
- [ ] commit push 시 specQueue 슬롯 32 미만 범위에서 주소 정확성 확인
- [ ] specQueue 슬롯 32 초과 후 commit push 시 잘못된 주소 가능성 검증 (FRAS-003)
- [ ] nsp/ssp mismatch 시 강제 보정 동작 확인

### Overflow
- [ ] overflow 상태 진입/해제 조건 검증 (`tosw - bos > 30`)
- [ ] overflow 중 redirect 억제 → 이후 예측 오류 발생 여부 확인

### writeBypass
- [ ] push 직후 다음 cycle에 `timingTop`이 push된 주소를 정확히 반영하는지 확인
- [ ] redirect+call 시 writeBypass 업데이트 및 다음 topRetAddr 확인

---

*분석 기준 파일: `src/main/scala/xiangshan/frontend/bpu/ras/Ras.scala`, `RasStack.scala`, `Bundles.scala`, `Parameters.scala`*
*코드 커밋: `bfbb21862` (branch: `kunminghu-v3`)*

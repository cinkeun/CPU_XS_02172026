# MicroRas 분석 리포트

> **분석 원칙**: code-based only. web-search 및 사전 지식 기반 추론 금지.
> **분석 대상**: `src/main/scala/xiangshan/frontend/bpu/ras/MicroRas.scala`
> **코어/레포/커밋**: XiangShan / `kunminghu-v3` / `bfbb21862`

---

## 1. 역할 및 위치

MicroRas는 BPU 파이프라인의 **S1 스테이지**에서 동작하는 조기 예측(early prediction) 전용 RAS이다.
주 RAS(`Ras.scala`)가 S3에서 push/pop을 확정하는 반면, MicroRas는 **S1~S3 사이의 in-flight 상태를 추적**하여 S1에 예측 return 주소(`retTarget`)와 유효 여부(`isCanUse`)를 제공한다.

- 실제 stack 메모리는 보유하지 않음
- 주 RAS의 `topRetAddr`를 입력받아 필요 시 그대로 전달하거나 보정하는 구조
- S1 예측 결과를 1 cycle 앞당기는 효과 → 주 RAS(S3)보다 **2 cycle 빠른** early prediction

---

## 2. 인터페이스

```scala
// Source: MicroRas.scala:57-62
val specIn:      MicroRasSpecIn  = Input(new MicroRasSpecIn)
val specOut:     MicroRasSpecOut = Output(new MicroRasSpecOut)
val hasRedirect: Bool            = Input(Bool())
val hasOverride: Bool            = Input(Bool())
val fullRetAddr: PrunedAddr      = Input(PrunedAddr(VAddrBits))
```

| 신호명 | 방향 | 설명 |
|---|---|---|
| `specIn.startPc` | Input | S1 fetch block 시작 PC |
| `specIn.cfiPosition` | Input | fetch block 내 CFI 위치 (offset) |
| `specIn.attribute.isCall` | Input | S1에서 감지된 CALL 여부 |
| `specIn.attribute.isReturn` | Input | S1에서 감지된 RET 여부 |
| `hasRedirect` | Input | 전역 redirect 신호 (misprediction 등) |
| `hasOverride` | Input | S3 override 신호 (S1~S2 flush 트리거) |
| `fullRetAddr` | Input | 주 RAS 현재 top 주소 (`ras.io.topRetAddr`) |
| `specOut.retTarget` | Output | S1에 제공하는 예측 return 주소 |
| `specOut.isCanUse` | Output | 예측 결과 유효 여부 |

---

## 3. 내부 상태 레지스터

```scala
// Source: MicroRas.scala:68-79
private val s2_hasPush = RegInit(false.B)
private val s2_hasPop  = RegInit(false.B)
private val s3_hasPush = RegInit(false.B)
private val s3_hasPop  = RegInit(false.B)
private val s2_retAddr = RegInit(0.U.asTypeOf(PrunedAddr(VAddrBits)))
private val s3_retAddr = RegInit(0.U.asTypeOf(PrunedAddr(VAddrBits)))
private val isCanUse   = RegInit(false.B)
private val topRetAddr = RegInit(0.U.asTypeOf(PrunedAddr(VAddrBits)))
private val redirectDelay1 = RegNext(io.hasRedirect, init = false.B)
```

| 레지스터 | 설명 |
|---|---|
| `s2_hasPush` / `s2_hasPop` | S2 stage에 push/pop 이벤트 대기 중 여부 |
| `s3_hasPush` / `s3_hasPop` | S3 stage에 push/pop 이벤트 대기 중 여부 |
| `s2_retAddr` | S1 CALL 감지 시 계산된 return 주소 (S2로 전달) |
| `s3_retAddr` | S2에서 S3로 전달된 return 주소 |
| `isCanUse` | 출력 예측 유효 여부 (registered) |
| `topRetAddr` | 출력 예측 주소 (registered) |
| `redirectDelay1` | `hasRedirect`의 1 cycle 지연 — redirect 복구 기간 감지용 |

---

## 4. Push 주소 계산

```scala
// Source: MicroRas.scala:85
private val specPushAddr = getCfiPcFromPosition(io.specIn.startPc, io.specIn.cfiPosition) + 2.U
```

- `getCfiPcFromPosition`: `HalfAlignHelper` 트레이트 제공 함수. `startPc`와 `cfiPosition`으로 실제 CALL 명령어 PC를 구함
- `+ 2.U`: CALL 명령어 다음 2바이트 (RVC compressed 기준 return 주소)

> 주 RAS(Ras.scala:70)의 계산 방식과 다름:
> 주 RAS: `(startPc & alignMask) + (cfiPosition << 1) + 2`
> MicroRas: `getCfiPcFromPosition(startPc, cfiPosition) + 2`
> 두 계산이 동일 결과를 내는지 검증 필요.

---

## 5. 파이프라인 stage 추적 로직

### S2 업데이트

```scala
// Source: MicroRas.scala:93-102
when(io.hasOverride || io.hasRedirect) {
  s2_hasPush := false.B
  s2_hasPop  := false.B
}.elsewhen(io.stageCtrl.s1_fire) {
  s2_hasPush := specPush
  s2_hasPop  := specPop
}.elsewhen(io.stageCtrl.s2_fire) {
  s2_hasPush := false.B
  s2_hasPop  := false.B
}
```

- `hasOverride` 또는 `hasRedirect` 시 S2 즉시 flush
- `s1_fire` 시 S1 결과를 S2로 래치
- `s2_fire` 시 S2 클리어 (S3으로 이동 완료)

### S3 업데이트

```scala
// Source: MicroRas.scala:107-119
when(io.hasRedirect) {
  s3_hasPush := false.B
  s3_hasPop  := false.B
}.elsewhen(io.stageCtrl.s2_fire) {
  s3_hasPush := s2_hasPush
  s3_hasPop  := s2_hasPop
}.elsewhen(io.stageCtrl.s3_fire) {
  s3_hasPush := false.B
  s3_hasPop  := false.B
}
```

- `hasRedirect`만 S3를 flush (주목: `hasOverride`는 S3를 flush하지 않음 — S3 결과는 override 시에도 유지)
- `s2_fire` 시 S2 → S3 전달
- `s3_fire` 시 S3 클리어

---

## 6. 예측 결과 결정 로직 (핵심)

출력 레지스터 `isCanUse`와 `topRetAddr`는 매 cycle 다음 case 중 하나로 업데이트:

### Case 1: `hasRedirect`

```scala
// Source: MicroRas.scala:135-138
when(io.hasRedirect) {
  isCanUse := false.B
}
```

- 주 RAS가 복구 중 → 예측 불가
- `topRetAddr`는 업데이트 없음 (이전 값 유지)

### Case 2: `hasOverride` (S3 override)

```scala
// Source: MicroRas.scala:139-146
.elsewhen(io.hasOverride) {
  isCanUse   := Mux(s3_hasPop, false.B, Mux(s3_hasPush, true.B, true.B))
  topRetAddr := Mux(s3_hasPop, 0.U..., Mux(s3_hasPush, s3_retAddr, io.fullRetAddr))
}
```

- S1~S2 flush, S3만 남음
- S3 pop이면 새 top 불명 → invalid
- S3 push이면 → `s3_retAddr`
- S3 중립이면 → `fullRetAddr` (주 RAS top)

### Case 3: S1 CALL (`s1_fire && specPush`)

```scala
// Source: MicroRas.scala:147-150
.elsewhen(io.stageCtrl.s1_fire && specPush) {
  isCanUse   := true.B
  topRetAddr := specPushAddr
}
```

- CALL 감지 → 다음 cycle의 RAS top = 이 CALL의 return 주소

### Case 4: S1 RET (`s1_fire && specPop`)

```scala
// Source: MicroRas.scala:151-173
.elsewhen(io.stageCtrl.s1_fire && specPop) {
  when(s2_hasPush) {
    // S2 push + S1 pop → 상쇄. S3 상태에 따라 결정
    isCanUse   := Mux(s3_hasPush, true.B, Mux(s3_hasPop, false.B, true.B))
    topRetAddr := Mux(s3_hasPush, s3_retAddr, Mux(s3_hasPop, 0.U..., io.fullRetAddr))
  }.elsewhen(s2_hasPop) {
    // 이중 pop → 불확정
    isCanUse := false.B
  }.elsewhen(s3_hasPush) {
    // S3 push + S1 pop → 상쇄. top은 현재 주 RAS top
    isCanUse   := true.B
    topRetAddr := io.fullRetAddr
  }.otherwise {
    // pop만 있고 새 top 미지
    isCanUse := false.B
  }
}
```

| 상황 | `isCanUse` | `topRetAddr` |
|---|---|---|
| `s2_hasPush` + `s3_hasPush` | true | `s3_retAddr` |
| `s2_hasPush` + `s3_hasPop` | false | — |
| `s2_hasPush` (S3 중립) | true | `fullRetAddr` |
| `s2_hasPop` | false | — |
| `s3_hasPush` (S2 중립) | true | `fullRetAddr` |
| otherwise | false | — |

### Case 5: S1 비-CFI (`s1_fire`, call도 ret도 아님)

```scala
// Source: MicroRas.scala:174-198
.elsewhen(io.stageCtrl.s1_fire) {
  when(s2_hasPush) {
    isCanUse   := true.B
    topRetAddr := s2_retAddr
  }.elsewhen(s2_hasPop) {
    isCanUse   := Mux(s3_hasPush, true.B, false.B)
    topRetAddr := Mux(s3_hasPush, io.fullRetAddr, 0.U...)
  }.elsewhen(s3_hasPop) {
    isCanUse := false.B
  }.elsewhen(s3_hasPush) {
    isCanUse   := true.B
    topRetAddr := s3_retAddr
  }.otherwise {
    isCanUse   := Mux(redirectDelay1, false.B, true.B)
    topRetAddr := Mux(redirectDelay1, 0.U..., io.fullRetAddr)
  }
}
```

| 상황 | `isCanUse` | `topRetAddr` |
|---|---|---|
| `s2_hasPush` | true | `s2_retAddr` |
| `s2_hasPop` + `s3_hasPush` | true | `fullRetAddr` |
| `s2_hasPop` (S3 중립/pop) | false | — |
| `s3_hasPop` (S2 중립) | false | — |
| `s3_hasPush` (S2 중립) | true | `s3_retAddr` |
| 완전 중립 + redirectDelay1 | false | — |
| 완전 중립 | true | `fullRetAddr` |

---

## 7. 출력 연결

```scala
// Source: MicroRas.scala:200-201
io.specOut.isCanUse  := isCanUse
io.specOut.retTarget := topRetAddr
```

출력은 모두 **registered** 값 → 항상 이전 cycle에 계산된 결과를 제공.
소비 측(Bpu.scala)에서는 `s1_prediction.attribute.isReturn && uras.io.specOut.isCanUse` 조건 시 `s1_prediction.target := uras.io.specOut.retTarget`으로 override.

---

## 8. Input-to-output latency

| 이벤트 | 입력 stage | 출력 유효 | latency |
|---|---|---|---|
| S1 CALL 감지 → 다음 S1 RET 예측 | S1 (`s1_fire`) | 다음 S1 cycle | 1 cycle |
| redirect → prediction 재유효화 | `hasRedirect` | redirect+2 cycle (redirectDelay1 해소 후) | 2 cycle |
| S3 override → S1 top 보정 | `hasOverride` | 해당 cycle 즉시 (등록 후 다음 S1) | 1 cycle |

---

## 9. 이슈

### MRAS-001
- **심각도**: `Low`
- **증상**: push 주소 계산 방식이 주 RAS와 다른 함수를 사용함
- **근거**:
  ```scala
  // MicroRas.scala:85
  val specPushAddr = getCfiPcFromPosition(io.specIn.startPc, io.specIn.cfiPosition) + 2.U
  // Ras.scala:70
  val specPushAddr = (specIn.startPc & alignMask) + (specIn.cfiPosition << 1.U).asUInt + 2.U
  ```
- **개선 제안**: 두 계산이 동일 결과인지 단위 테스트로 검증 필요. 다르다면 MicroRas가 잘못된 주소를 예측할 수 있음.

### MRAS-002
- **심각도**: `Medium`
- **증상**: `hasOverride` 시 `isCanUse`가 S3 pop 여부와 무관하게 `true`가 되는 중복 Mux 경로 존재
- **근거**:
  ```scala
  // MicroRas.scala:145
  isCanUse := Mux(s3_hasPop, false.B, Mux(s3_hasPush, true.B, true.B))
  //                                                         ^^^^^^^^^^^
  // s3_hasPush=true 와 false(S3 중립) 모두 true → 두 번째 Mux 불필요
  ```
- **영향**: 논리 오류는 아니나, 코드 의도 파악이 어려움. S3 중립 시 `fullRetAddr`를 신뢰할 수 있는지 별도 검토 필요.

### MRAS-003
- **심각도**: `Info`
- **증상**: `hasOverride` 케이스에서 S3 중립 시 `topRetAddr = io.fullRetAddr`를 사용하나, override 직후 주 RAS 상태가 확정된 상태인지 보장 코드 없음
- **근거**: `hasOverride` 조건에서 `fullRetAddr`는 주 RAS의 현재 `timingTop`이나, override는 S3에서 발생하므로 직전 cycle에 push/pop이 있었을 경우 `timingTop`이 아직 반영 중일 수 있음

---

## 10. 검증 체크리스트

- [ ] S1 CALL → 다음 cycle S1 RET 예측에서 `retTarget == specPushAddr` 확인
- [ ] `hasRedirect` 직후 cycle에 `isCanUse == false` 확인
- [ ] `redirectDelay1` 해소 후 `isCanUse`가 `fullRetAddr`로 복원 확인
- [ ] `hasOverride` 시 `s3_hasPop=true`이면 `isCanUse == false` 확인
- [ ] MicroRas `specPushAddr`와 주 RAS `specPushAddr`가 같은 값인지 동일 CALL 입력으로 비교
- [ ] `s2_hasPush` + `s1_specPop` 상쇄 케이스에서 올바른 `fullRetAddr` 사용 확인
- [ ] 연속 redirect 2회 발생 시 `isCanUse` 연속 false 확인

---

*분석 기준 파일: `src/main/scala/xiangshan/frontend/bpu/ras/MicroRas.scala`*
*코드 커밋: `bfbb21862` (branch: `kunminghu-v3`)*

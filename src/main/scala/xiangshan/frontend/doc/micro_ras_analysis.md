# MicroRas Analysis Report

> **Analysis Principle**: code-based only. No web-search and prior knowledge-based inferences.
> **Analysis target**: `src/main/scala/xiangshan/frontend/bpu/ras/MicroRas.scala`
> **Core/Repo/Commit**: XiangShan / `kunminghu-v3` / `bfbb21862`

---

## 1. Role and location

MicroRas is a RAS dedicated to early prediction that operates in the **S1 stage** of the BPU pipeline.
While the main RAS (`Ras.scala`) confirms the push/pop in S3, MicroRas **tracks the in-flight state between S1 and S3** and provides the predicted return address (`retTarget`) and validity (`isCanUse`) to S1.

- Does not hold actual stack memory
- Structure that receives `topRetAddr` from the main RAS and transmits or corrects it as is when necessary
- The effect of advancing the S1 prediction result by 1 cycle → Early prediction **2 cycles earlier** than the main RAS (S3)

---

## 2. Interface

```scala
// Source: MicroRas.scala:57-62
val specIn:      MicroRasSpecIn  = Input(new MicroRasSpecIn)
val specOut:     MicroRasSpecOut = Output(new MicroRasSpecOut)
val hasRedirect: Bool            = Input(Bool())
val hasOverride: Bool            = Input(Bool())
val fullRetAddr: PrunedAddr      = Input(PrunedAddr(VAddrBits))
```

| signal name | direction | Description |
|---|---|---|
| `specIn.startPc` | Input | S1 fetch block start PC |
| `specIn.cfiPosition` | Input | CFI location (offset) in fetch block |
| `specIn.attribute.isCall` | Input | Whether CALL is detected in S1 |
| `specIn.attribute.isReturn` | Input | Whether RET detected in S1 |
| `hasRedirect` | Input | global redirect signals (misprediction, etc.) |
| `hasOverride` | Input | S3 override signal (S1~S2 flush trigger) |
| `fullRetAddr` | Input | Main RAS current top address (`ras.io.topRetAddr`) |
| `specOut.retTarget` | Output | Predicted return address provided to S1 |
| `specOut.isCanUse` | Output | Whether the prediction result is valid |

---

## 3. Internal status register

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

| register | Description |
|---|---|
| `s2_hasPush` / `s2_hasPop` | Whether push/pop event is waiting for S2 stage |
| `s3_hasPush` / `s3_hasPop` | Whether push/pop event is waiting for S3 stage |
| `s2_retAddr` | Return address calculated when detecting S1 CALL (passed to S2) |
| `s3_retAddr` | return address passed from S2 to S3 |
| `isCanUse` | Output prediction validity (registered) |
| `topRetAddr` | Output prediction address (registered) |
| `redirectDelay1` | 1 cycle delay of `hasRedirect` — for detecting redirect recovery period |

---

## 4. Push address calculation

```scala
// Source: MicroRas.scala:85
private val specPushAddr = getCfiPcFromPosition(io.specIn.startPc, io.specIn.cfiPosition) + 2.U
```

- `getCfiPcFromPosition`: `HalfAlignHelper` trait provided function. Obtain the actual CALL command PC with `startPc` and `cfiPosition`
- `+ 2.U`: 2 bytes following CALL command (return address based on RVC compressed)

> Different from the calculation method in the main RAS (Ras.scala:70):
> Primary RAS: `(startPc & alignMask) + (cfiPosition << 1) + 2`
> MicroRas: `getCfiPcFromPosition(startPc, cfiPosition) + 2`
> Need to verify that the two calculations produce the same results.

---

## 5. Pipeline stage tracking logic

### S2 Update

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

- S2 is flushed immediately when `hasOverride` or `hasRedirect`
- Latch S1 result to S2 when `s1_fire`
- Clear S2 at `s2_fire` (move to S3 completed)

### S3 updates

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

- Only `hasRedirect` flushes S3 (Note: `hasOverride` does not flush S3 — S3 results are maintained even when overriding)
- S2 → S3 delivery at `s2_fire`
- Clear S3 at `s3_fire`

---

## 6. Prediction result decision logic (core)

Output registers `isCanUse` and `topRetAddr` are updated every cycle in one of the following cases:

### Case 1: `hasRedirect`

```scala
// Source: MicroRas.scala:135-138
when(io.hasRedirect) {
  isCanUse := false.B
}
```

- Main RAS is recovering → unpredictable
- `topRetAddr` does not update (keeps previous value)

### Case 2: `hasOverride` (S3 override)

```scala
// Source: MicroRas.scala:139-146
.elsewhen(io.hasOverride) {
  isCanUse   := Mux(s3_hasPop, false.B, Mux(s3_hasPush, true.B, true.B))
  topRetAddr := Mux(s3_hasPop, 0.U..., Mux(s3_hasPush, s3_retAddr, io.fullRetAddr))
}
```

- S1~S2 flush, only S3 remains
- If S3 pops, the new top is unknown → invalid
- If S3 push → `s3_retAddr`
- If S3 is neutral → `fullRetAddr` (main RAS top)

### Case 3: S1 CALL (`s1_fire && specPush`)

```scala
// Source: MicroRas.scala:147-150
.elsewhen(io.stageCtrl.s1_fire && specPush) {
  isCanUse   := true.B
  topRetAddr := specPushAddr
}
```

- CALL detection → RAS top of next cycle = return address of this CALL

### Case 4: S1 RET (`s1_fire && specPop`)

```scala
// Source: MicroRas.scala:151-173
.elsewhen(io.stageCtrl.s1_fire && specPop) {
  when(s2_hasPush) {
// S2 push + S1 pop → offset. Determined by S3 state
    isCanUse   := Mux(s3_hasPush, true.B, Mux(s3_hasPop, false.B, true.B))
    topRetAddr := Mux(s3_hasPush, s3_retAddr, Mux(s3_hasPop, 0.U..., io.fullRetAddr))
  }.elsewhen(s2_hasPop) {
// double pop → indeterminate
    isCanUse := false.B
  }.elsewhen(s3_hasPush) {
// S3 push + S1 pop → offset. top is the current main RAS top
    isCanUse   := true.B
    topRetAddr := io.fullRetAddr
  }.otherwise {
// There is only pop and the new top is unknown
    isCanUse := false.B
  }
}
```

| Situation | `isCanUse` | `topRetAddr` |
|---|---|---|
| `s2_hasPush` + `s3_hasPush` | true | `s3_retAddr` |
| `s2_hasPush` + `s3_hasPop` | false | — |
| `s2_hasPush` (S3 neutral) | true | `fullRetAddr` |
| `s2_hasPop` | false | — |
| `s3_hasPush` (S2 neutral) | true | `fullRetAddr` |
| otherwise | false | — |

### Case 5: S1 non-CFI (`s1_fire`, neither call nor ret)

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

| Situation | `isCanUse` | `topRetAddr` |
|---|---|---|
| `s2_hasPush` | true | `s2_retAddr` |
| `s2_hasPop` + `s3_hasPush` | true | `fullRetAddr` |
| `s2_hasPop` (S3 neutral/pop) | false | — |
| `s3_hasPop` (S2 neutral) | false | — |
| `s3_hasPush` (S2 neutral) | true | `s3_retAddr` |
| Full neutral + redirectDelay1 | false | — |
| Completely neutral | true | `fullRetAddr` |

---

## 7. Output connection

```scala
// Source: MicroRas.scala:200-201
io.specOut.isCanUse  := isCanUse
io.specOut.retTarget := topRetAddr
```

All outputs are **registered** values ​​→ always provide the results calculated in the previous cycle.
On the consumption side (Bpu.scala), if the condition is `s1_prediction.attribute.isReturn && uras.io.specOut.isCanUse`, override it with `s1_prediction.target := uras.io.specOut.retTarget`.

---

## 8. Input-to-output latency

| Event | input stage | output valid | latency |
|---|---|---|---|
| Detect S1 CALL → Predict next S1 RET | S1 (`s1_fire`) | Next S1 cycle | 1 cycle |
| redirect → re-validate prediction | `hasRedirect` | redirect+2 cycle (after redirectDelay1 is resolved) | 2 cycle |
| S3 override → S1 top correction | `hasOverride` | Immediately after registration (next S1 after registration) | 1 cycle |

---

## 9. Issue

### MRAS-001
- **Severity**: `Low`
- **Symptom**: The push address calculation method uses a different function than the main RAS.
- **reason**:
  ```scala
  // MicroRas.scala:85
  val specPushAddr = getCfiPcFromPosition(io.specIn.startPc, io.specIn.cfiPosition) + 2.U
  // Ras.scala:70
  val specPushAddr = (specIn.startPc & alignMask) + (specIn.cfiPosition << 1.U).asUInt + 2.U
  ```
- **Improvement Suggestion**: Requires verification with unit tests that two calculations result in the same result. If they are different, MicroRas may predict the wrong address.

### MRAS-002
- **Severity**: `Medium`
- **Symptom**: When `hasOverride`, there is a duplicate Mux path that causes `isCanUse` to become `true` regardless of whether S3 pops or not.
- **reason**:
  ```scala
  // MicroRas.scala:145
  isCanUse := Mux(s3_hasPop, false.B, Mux(s3_hasPush, true.B, true.B))
  //                                                         ^^^^^^^^^^^
// s3_hasPush=true and false (S3 neutral) both true → second Mux not needed
  ```
- **Impact**: It is not a logical error, but it is difficult to understand the intent of the code. A separate review is required to see if `fullRetAddr` can be trusted when S3 is neutral.

### MRAS-003
- **Severity**: `Info`
- **Symptom**: In `hasOverride` case, `topRetAddr = io.fullRetAddr` is used during S3 neutral, but there is no code to ensure that the main RAS state is confirmed immediately after override.
- **Rationale**: In the `hasOverride` condition, `fullRetAddr` is the current `timingTop` of the main RAS, but since override occurs in S3, if there was a push/pop in the previous cycle, `timingTop` may still be reflected.

---

## 10. Verification Checklist

- [ ] S1 CALL → Check `retTarget == specPushAddr` in next cycle S1 RET prediction
- [ ] Check `isCanUse == false` in the cycle immediately after `hasRedirect`
- [ ] Confirm that `isCanUse` is restored to `fullRetAddr` after resolving `redirectDelay1`
- [ ] When `hasOverride`, if `s3_hasPop=true`, check `isCanUse == false`
- [ ] Compare MicroRas `specPushAddr` and main RAS `specPushAddr` with the same CALL input to see if they are the same value.
- [ ] Check correct use of `fullRetAddr` in `s2_hasPush` + `s1_specPop` offset case
- [ ] When redirect occurs twice in succession, `isCanUse` is continuously confirmed as false.

---

*Analysis standard file: `src/main/scala/xiangshan/frontend/bpu/ras/MicroRas.scala`*
*Code commit: `bfbb21862` (branch: `kunminghu-v3`)*

# ubtb (MicroBtb) 분석

> 분석 기준: BTB_analysis_rule.md
> 분석 대상: `src/main/scala/xiangshan/frontend/bpu/ubtb/`
> Code-based only — 사전 지식/web-search 사용 금지

---

## 1.1 BTB 종류 및 역할

MicroBtb는 **Block-type BTB**로, 현재 입력 fetch block에서 첫 번째 taken branch를 예측한다.
Fully-associative 구조이며, hit 시 항상 taken(`io.prediction.bits.taken := s1_hit`)으로 처리한다.
예측 거리는 **현재 block의 "다음 block"** 이다 (lookahead 아님).

| BTB  | Type   | Block Width          | Predict Distance                | 설명 |
|------|--------|----------------------|---------------------------------|------|
| ubtb | Block  | FetchBlockSize (bytes) | 현재 block → 다음 block (s1 출력) | 32-entry fully-associative, 항상 taken 예측 |

```
// Source: ubtb/MicroBtb.scala:78-83
// we do always-taken prediction in ubtb
io.prediction.valid            := s1_hit
io.prediction.bits.taken       := s1_hit
io.prediction.bits.cfiPosition := s1_hitEntry.slot1.position
io.prediction.bits.target      := getFullTarget(s1_startPc, s1_hitEntry.slot1.target, s1_hitEntry.slot1.targetCarry)
io.prediction.bits.attribute   := s1_hitEntry.slot1.attribute
```

---

## 1.2 BTB memory spec

MicroBtb는 **SRAM이 아닌 레지스터 배열**로 구현된다.

```scala
// Source: ubtb/MicroBtb.scala:49
private val entries = RegInit(VecInit(Seq.fill(NumEntries)(0.U.asTypeOf(new MicroBtbEntry))))
```

| Memory    | Depth       | Width (bit)                         | Banks | Read Ports | Write Ports |
|-----------|-------------|-------------------------------------|-------|------------|-------------|
| entries   | 32 (NumEntries) | tag(22) + usefulCnt(2) + slot1 + slot2 | 1 (full-assoc) | 32 (병렬 전체 read) | 1 (선택된 entry write-back) |

- slot1 width: position(CfiPositionWidth) + attribute(4) + target(22) + isStaticTarget(1)
- slot2 width: valid(1) + position(CfiPositionWidth) + attribute(4) + target(22) + taken(1)
- BranchAttribute: branchType(2-bit EnumUInt(4)) + rasAction(2-bit EnumUInt(4)) = 4 bit
- CfiPositionWidth = FetchBlockSizeWidth = log2Ceil(FetchBlockSize) [파라미터 의존]

---

## 1.3 BTB memory entry 설명

```scala
// Source: ubtb/Bundles.scala:32-66
class MicroBtbEntry(implicit p: Parameters) extends MicroBtbBundle {
  class SlotBase extends Bundle {
    val position: UInt  = UInt(CfiPositionWidth.W)     // fetch block 내 branch 위치
    val attribute: BranchAttribute = new BranchAttribute
    val target: UInt    = UInt(TargetWidth.W)           // 22-bit partial target
    val targetCarry: Option[TargetCarry] = if (EnableTargetFix) Option(new TargetCarry) else None
  }
  class Slot1 extends SlotBase {
    val isStaticTarget: Bool = Bool()  // 항상 같은 target으로 이동하는지 여부
  }
  class Slot2 extends SlotBase {
    val valid: Bool = Bool()   // slot2 유효 여부
    val taken: Bool = Bool()   // slot2 branch 예측 방향
  }
  def valid: Bool = !usefulCnt.isSaturateNegative  // usefulCnt > min → valid
  val tag: UInt       = UInt(TagWidth.W)            // 22-bit partial vTag
  val usefulCnt: SaturateCounter = UsefulCounter()  // 2-bit 포화 카운터 (useful 여부 판단)
  val slot1: Slot1 = new Slot1
  val slot2: Slot2 = new Slot2
}
```

| Field Name            | Width (bit)         | Description |
|-----------------------|---------------------|-------------|
| tag                   | 22 (TagWidth)       | PC[instOffsetBits + TagWidth - 1 : instOffsetBits], tag 비교용 |
| usefulCnt             | 2 (UsefulCntWidth)  | 포화 카운터; 최솟값 = invalid entry |
| slot1.position        | CfiPositionWidth    | fetch block 내 첫 번째 branch 위치 |
| slot1.attribute       | 4                   | BranchAttribute (branchType 2-bit + rasAction 2-bit) — 상세는 아래 참고 |
| slot1.target          | 22 (TargetWidth)    | target 하위 비트; 상위는 startPc에서 파생 |
| slot1.isStaticTarget  | 1                   | true = 동일 target만 본 경우 (dynamic target tracking) |
| slot1.targetCarry     | 2 (opt)             | EnableTargetFix=false 기본값 → 미포함 |
| slot2.valid           | 1                   | slot2 사용 여부 (현재 TODO 상태) |
| slot2.position        | CfiPositionWidth    | 두 번째 branch 위치 |
| slot2.attribute       | 4                   | 두 번째 branch 속성 |
| slot2.target          | 22                  | 두 번째 branch target 하위 |
| slot2.taken           | 1                   | 두 번째 branch 방향 |

### BranchAttribute 인코딩 (4-bit)

```scala
// Source: bpu/Bundles.scala:37-140
class BranchAttribute extends Bundle {
  val branchType: UInt = BranchAttribute.BranchType()  // bits[1:0]
  val rasAction:  UInt = BranchAttribute.RasAction()   // bits[3:2]
}

object BranchAttribute {
  object BranchType extends EnumUInt(4) {  // width = ceil(log2(4)) = 2 bit
    def None:        UInt = 0.U  // 0b00 — branch 아님 (fallthrough)
    def Conditional: UInt = 1.U  // 0b01 — beq/bne/blt/bge/bltu/bgeu
    def Direct:      UInt = 2.U  // 0b10 — j/jal (고정 offset)
    def Indirect:    UInt = 3.U  // 0b11 — jr/jalr (레지스터 기반)
  }
  object RasAction extends EnumUInt(4) {  // width = 2 bit
    def popBit  = 0  // bit[0]: pop (return)
    def pushBit = 1  // bit[1]: push (call)
    def None:       UInt = 0.U  // 0b00 — RAS 동작 없음
    def Pop:        UInt = 1.U  // 0b01 — return (rs=x1/x5, rd≠rs)
    def Push:       UInt = 2.U  // 0b10 — call  (rd=x1/x5)
    def PopAndPush: UInt = 3.U  // 0b11 — return & call (rs=rd=x1/x5, rs≠rd)
  }
}
```

**bit 레이아웃 (Chisel Bundle: 먼저 선언된 필드가 LSB)**

```
bit[3]  bit[2]  bit[1]  bit[0]
  rasAction[1]    rasAction[0]    branchType[1]   branchType[0]
  (push)          (pop)
```

**유효한 조합 목록 (decode 결과)**

| 이름          | branchType [1:0] | rasAction [3:2] | 4-bit (hex) | 조건 (RISC-V)                               | needIttage |
|---------------|------------------|-----------------|-------------|---------------------------------------------|------------|
| None          | 00               | 00              | 0x0         | branch 아님                                 | false      |
| Conditional   | 01               | 00              | 0x1         | beq/bne/blt/bge/bltu/bgeu                  | false      |
| OtherDirect   | 10               | 00              | 0x2         | j/jal (rd≠x1/x5 또는 RVC j/jr)             | false      |
| DirectCall    | 10               | 10              | 0xA         | jal with rd=x1/x5                           | false      |
| OtherIndirect | 11               | 00              | 0x3         | jalr, rd≠x1/x5, rs≠x1/x5                  | **true**   |
| Return        | 11               | 01              | 0x7         | jalr, rs=x1/x5, rd≠rs                      | false      |
| IndirectCall  | 11               | 10              | 0xB         | jalr, rd=x1/x5, rs≠x1/x5 (또는 rs≠rd)     | **true**   |
| ReturnAndCall | 11               | 11              | 0xF         | jalr, rd=x1/x5, rs=x1/x5, rs≠rd            | false      |

```scala
// Source: bpu/Bundles.scala:60
def needIttage: Bool = isIndirect && !hasPop  // OtherIndirect + IndirectCall만 해당
// hasPop = rasAction(popBit) = rasAction[0]
```

**predictor별 attribute 활용 요약**

| 판단 메서드        | 조건                        | ubtb 활용 위치 |
|--------------------|-----------------------------|----------------|
| `isConditional`    | branchType == 01            | taken 여부를 utage로 override |
| `isDirect`         | branchType == 10            | 항상 taken으로 예측 |
| `isIndirect`       | branchType == 11            | 항상 taken; needIttage이면 s3에서 ITTage로 target 교체 |
| `isReturn`/`hasPop`| rasAction[0] == 1           | s1에서 uras.retTarget으로 target 교체 |
| `isCall`/`hasPush` | rasAction[1] == 1           | RAS push (ras 모듈에서 처리) |

---

## 1.4 Pair prediction unit

```scala
// Source: bpu/Bpu.scala:266-277
private val s1_btbPrediction = VecInit(ubtb.io.prediction) ++ abtb.io.prediction
private val s1_utageHitMask = VecInit(s1_btbPrediction.map { pred =>
  pred.valid && utage.io.prediction.valid && utage.io.prediction.bits.cfiPosition === pred.bits.cfiPosition
})
private val s1_takenMask = VecInit(s1_btbPrediction.zipWithIndex.map { case (pred, i) =>
  val utageHit   = s1_utageHitMask(i)
  val utageTaken = utage.io.prediction.bits.taken
  pred.valid && (
    pred.bits.attribute.isDirect ||
    pred.bits.attribute.isIndirect ||
    pred.bits.attribute.isConditional && Mux(utageHit, utageTaken, pred.bits.taken)
  )
})
```

| BTB  | Paired Unit       | 역할                              | 결합 방식 |
|------|-------------------|-----------------------------------|-----------|
| ubtb | MicroTage (utage) | 조건부 branch 방향 결정 (override) | ubtb hit + position 일치 시 utage.taken 사용 |
| ubtb | -                 | 직접/간접 branch 타겟              | ubtb의 target을 그대로 사용 |

- MicroTage가 hit (position 일치)하면 ubtb의 taken 비트를 utage의 taken으로 교체
- MicroRas (uras)도 s1에서 return address를 교체할 수 있음

```scala
// Source: bpu/Bpu.scala:307-309
private val s1_isRet = s1_prediction.attribute.isReturn
when(s1_isRet && uras.io.specOut.isCanUse) {
  s1_prediction.target := uras.io.specOut.retTarget
}
```

---

## 1.5 다음 예측 pseudocode

```text
onPredict(startPc):
  // s0: latch input PC
  s1_startPc = RegEnable(startPc, s0_fire)

  // s1: full-associative tag compare
  s1_tag = getTag(s1_startPc)         // PC[instOffsetBits + TagWidth - 1 : instOffsetBits]
  s1_hitOH = entries.map(e => e.valid && e.tag === s1_tag)  // valid = usefulCnt > min
  assert(PopCount(s1_hitOH) <= 1)     // 최대 1-hot

  if s1_hitOH.orR:
    hit = true
    hitEntry = entries(OHToUInt(s1_hitOH))
    output.valid      = true
    output.taken      = true           // 항상 taken
    output.cfiPosition = hitEntry.slot1.position
    output.target     = getFullTarget(s1_startPc, hitEntry.slot1.target)
    output.attribute  = hitEntry.slot1.attribute
  else:
    output.valid = false

  // BPU top에서 utage가 conditional branch 방향 override 가능
  if utage.prediction.valid && utage.position == output.cfiPosition:
    output.taken (effective) = utage.taken

  // replacer touch on predict hit
  replacer.predTouch = (s1_hit && s1_fire, s1_hitIdx)
```

---

## 1.6 Input-to-output latency 및 throughput

| BTB  | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|------|-------------|--------------|-----------------|--------------------------|
| ubtb | s0 (startPc) | s1 (prediction valid) | 1 | 1 |

- s0에서 startPc를 RegEnable로 래치하고, s1에서 32-entry 병렬 비교 후 1사이클 내 출력
- 레지스터 기반이므로 SRAM read latency 없음 → 1-cycle 예측

---

## 1.7 Pipeline stage 위치

| Signal               | Produced @ Stage | Consumed @ Stage | Timing Note |
|----------------------|------------------|------------------|-------------|
| s0_startPc           | BPU s0           | ubtb s0          | io.startPc 직결 |
| s1_startPc           | ubtb s0 → s1     | ubtb s1          | `RegEnable(s0_startPc, s0_fire)` |
| s1_hitOH / s1_hit    | ubtb s1          | ubtb s1          | 레지스터 entries 조합 논리 |
| io.prediction        | ubtb s1          | BPU s1           | valid := s1_hit |
| s1_btbPrediction[0]  | BPU s1           | BPU s1           | VecInit(ubtb.io.prediction) ++ abtb.io.prediction |

```scala
// Source: ubtb/MicroBtb.scala:69-76
private val s1_startPc = RegEnable(s0_startPc, s0_fire)
private val s1_tag     = getTag(s1_startPc)
private val s1_hitOH   = VecInit(entries.map(e => e.valid && e.tag === s1_tag)).asUInt
private val s1_hit     = s1_hitOH.orR
private val s1_hitIdx  = OHToUInt(s1_hitOH)
private val s1_hitEntry = entries(s1_hitIdx)
```

---

## 1.8 BTB memory indexing hashing 방법

### 구조: 완전 연관 (Fully-Associative), index 없음

ubtb는 완전 연관 register-file 구조이므로 setIdx / bankIdx가 없다.
32개 entry 전체를 병렬 tag 비교하여 hit를 결정한다.

```scala
// Source: ubtb/Helpers.scala:25-34
val addrFields = AddrField(
  Seq(
    ("instOffset", instOffsetBits),  // PC bit [0:0], 항상 0 (2B 정렬)
    ("tag", TagWidth)                // PC bit [22:1]  (TagWidth=22)
  ),
  maxWidth = Option(VAddrBits),
  extraFields = Seq(
    ("targetLower", instOffsetBits, TargetWidth)  // target bit [22:1]
  )
)
```

### tag 계산식

```scala
// Source: ubtb/Helpers.scala:36-37
def getTag(pc: PrunedAddr): UInt =
  addrFields.extract("tag", pc)
// ↑ tag = pc[instOffsetBits + TagWidth - 1 : instOffsetBits]
//       = pc[1 + 22 - 1 : 1] = pc[22:1]
```

- history 사용: **없음**
- hash 없음 (XOR/fold 미적용), PC 단순 bit extraction

| Path | Field | Formula | PC Bits | History | Note |
|------|-------|---------|---------|---------|------|
| Predict (s1) | tag | pc[22:1] | [22:1] | 없음 | s1_startPc |
| Train (t0) | tag | pc[22:1] | [22:1] | 없음 | fastTrain.startPc |

> Predict path와 Train path 모두 동일한 formula 사용.
> instOffset (bit [0]) 은 2B 정렬로 항상 0 — 분류에 사용하지 않음.

---

## 1.9 Training 방법

### fast-train trigger 조건

```scala
// Source: ubtb/MicroBtb.scala:102-117
if (UseFastTrain) {
  t0_fire        := io.fastTrain.get.valid && io.enable   // ← 조건 핵심
  t0_startPc     := io.fastTrain.get.bits.startPc
  t0_actualTaken := io.fastTrain.get.bits.finalPrediction.taken
  t0_position    := io.fastTrain.get.bits.finalPrediction.cfiPosition
  t0_fullTarget  := io.fastTrain.get.bits.finalPrediction.target
  t0_attribute   := io.fastTrain.get.bits.finalPrediction.attribute
} else {
  // slow mode: mispredict가 있는 FTQ commit에서만 train
  t0_fire        := io.stageCtrl.t0_fire && io.train.mispredictBranch.valid && io.enable
  ...
}
```

```scala
// Source: bpu/Bpu.scala:182-188
private val fastTrain = Wire(Valid(new BpuFastTrain))
fastTrain.valid                := s3_valid               // ← BPU s3 파이프라인이 유효한 매 사이클
fastTrain.bits.startPc         := s3_startPc
fastTrain.bits.finalPrediction := s3_prediction          // mbtb+Tage+Sc+ITTage+RAS 최종 결합 결과
fastTrain.bits.abtbMeta        := s3_abtbMeta
fastTrain.bits.utageMeta       := s3_utageMeta
fastTrain.bits.hasOverride     := s3_override
```

**fast-train이 fire하는 조건: `s3_valid == true` (BPU s3 파이프라인 유효)**

즉, ubtb는 **taken/not-taken, mispredict 여부와 무관하게** BPU s3가 valid인 매 사이클 train한다.
`t0_actualTaken = finalPrediction.taken` 이므로 실제 어떤 동작을 취하는지는 taken 여부로 분기:

| 조건                        | t0_fire | t0_actualTaken | t1에서의 동작 |
|-----------------------------|---------|----------------|---------------|
| s3_valid && prediction.taken  | true  | true           | hit → usefulCnt 증감 / miss → allocate |
| s3_valid && !prediction.taken | true  | false          | hit → usefulCnt 감소 또는 re-init / miss → **아무 것도 안 함** (allocate 조건 불충족) |
| !s3_valid                   | false   | —              | train 없음 |

> **slow mode 비교**: `io.stageCtrl.t0_fire && mispredictBranch.valid` — mispredict가 있는 commit만 train.
> FIXME 주석(`// FIXME: not sure if first mispredict is the best, maybe first taken?`)이 있어 아직 설계 확정 전.

---

### Train pipeline: t0 → t1

```scala
// Source: ubtb/MicroBtb.scala:162-228
t1_fire := RegNext(t0_fire, false.B)   // t0의 1-cycle 지연

// t1에서 결정되는 동작 (3가지 케이스)
when(t1_fire) {
  when(!t1_hit) {
    // case 1: miss → taken인 경우만 새 entry 할당 (initEntryIfNotUseful(true.B))
    initEntryIfNotUseful(true.B)
  }.elsewhen(!t1_hitAttributeSame || !t1_hitPositionSame || !t1_hitTargetSame || !t1_actualTaken) {
    // case 2: hit + (position/attribute/target 불일치 OR not-taken)
    //   - notUseful이면 새 entry로 재초기화
    //   - useful이면 usefulCnt 감소
    //   - target 불일치이면 isStaticTarget := false
    initEntryIfNotUseful(t1_hitNotUseful)
    when(!t1_hitTargetSame) { t1_updatedEntry.slot1.isStaticTarget := false.B }
  }.otherwise {
    // case 3: hit + 모든 필드 일치 + taken → usefulCnt 증가
    t1_updatedEntry.usefulCnt := t1_hitEntry.usefulCnt.getIncrease()
  }
}

// write-back: hit이거나 allocate인 경우만 entries에 반영
t1_allocate  := !t1_hit && t1_actualTaken          // miss + taken일 때만 신규 할당
t1_updateIdx := Mux(t1_hit, t1_hitIdx, replacer.io.victim)
when(t1_fire && (t1_hit || t1_allocate)) {
  entries(t1_updateIdx) := t1_updatedEntry
}
```

**entry 초기화 시 usefulCnt**: `resetSaturatePositive()` (최댓값으로 시작, 이후 맞으면 증가/틀리면 감소).

---

### Victim entry 결정 (MicroBtbReplacer)

```scala
// Source: ubtb/MicroBtbReplacer.scala:38-52
private val replacer = ReplacementPolicy.fromString(Replacer, NumEntries)  // PLRU (NumEntries=32)

// Step 1: usefulCnt가 최솟값(isSaturateNegative)인 entry 탐색
private val notUsefulVec = VecInit(io.usefulCnt.map(_.isSaturateNegative))
private val notUseful    = notUsefulVec.reduce(_ || _)
private val notUsefulIdx = PriorityEncoder(notUsefulVec)  // index 0부터 첫 번째 not-useful

// Step 2: not-useful이 있으면 우선 사용, 없으면 PLRU victim 사용
io.victim := Mux(notUseful, notUsefulIdx, replacer.way)

// PLRU 상태 갱신: predict hit과 train touch 둘 다 반영
replacer.access(Seq(io.predTouch, io.trainTouch))
// predTouch:  valid = s1_hit && s1_fire   (예측 시 hit entry touch)
// trainTouch: valid = t1_fire,  bits = t1_updateIdx  (train 시 갱신된 entry touch)
```

**victim 선택 우선순위 요약**

```
priority 1 (우선): notUseful entry 중 가장 낮은 index (PriorityEncoder)
priority 2 (fallback): PLRU replacer가 지목한 way
```

| 상황                               | victim 출처     | 비고 |
|------------------------------------|-----------------|------|
| 하나 이상의 usefulCnt가 최솟값     | PriorityEncoder | index 0부터 스캔, 첫 번째 not-useful 선택 |
| 모든 entry가 useful                | PLRU .way       | predict touch + train touch로 갱신된 PLRU 트리 기반 |

> victim은 `t1_allocate (= !t1_hit && t1_actualTaken)` 조건이 true일 때만 실제로 사용된다.
> hit인 경우에는 victim을 무시하고 `t1_hitIdx`에 직접 write-back.

---

### 연속 train 충돌 처리 (t0-t1 hazard)

t0와 t1이 연속 발동될 때 데이터 hazard 발생 가능:

```scala
// Source: ubtb/MicroBtb.scala:128-148
// 문제 1: t1이 write 중인 entry를 t0가 아직 반영 안 된 entries[]에서 읽어 miss로 오판 → 잘못된 allocate
// 문제 2: t1이 t0의 hit entry를 victim으로 선택하여 교체 중 → t0가 miss인 척 해야 함
private val t0_hitT1Update = Wire(Bool())
private val t0_hitT1Victim = t1_fire && t0_realHitIdx === replacer.io.victim && t1_allocate

// 최종 hit 판정 보정
private val t0_hit = t0_realHit && !t0_hitT1Victim || t0_hitT1Update

// t0_hitT1Update: t1이 같은 tag를 갱신 중이면 t0는 t1의 updatedEntry를 "미리 본" 것으로 처리
t0_hitT1Update := t1_fire && t0_tag === t1_tag && (t1_hit || t1_allocate)
// → t0_hitEntry = t1_updatedEntry (wire forwarding)
```

| 시나리오                                      | 보정 신호         | 효과 |
|-----------------------------------------------|-------------------|------|
| t1이 같은 tag entry를 갱신 중 (t0 뒤따라 도착) | `t0_hitT1Update`  | t0가 t1의 `t1_updatedEntry`를 hit으로 간주 → false miss 방지 |
| t1이 t0의 hit entry를 victim으로 교체 중       | `t0_hitT1Victim`  | t0의 hit를 강제로 miss 처리 → false hit 방지 |

---

### Training 입력 정보 경로

```scala
// Source: ubtb/Bundles.scala:68-70
class MicroBtbMeta(implicit p: Parameters) extends MicroBtbBundle {
  // seems no meta is needed now, reserved for future use
}
```

| Trigger                              | Required Info                                                      | Info Path | Write Port / Conflict Handling |
|--------------------------------------|---------------------------------------------------------------------|-----------|--------------------------------|
| `s3_valid` (fast-train, 매 s3 사이클) | s3_startPc, finalPrediction (taken, position, target, attribute)   | BPU 내부 `fastTrain` wire (`s3_*` 기반) | 없음 (레지스터 1-port, t1 1회/cycle) |
| `mispredictBranch.valid` (slow mode) | startPc, mispredictBranch (taken, position, target, attribute)     | FTQ→BPU `io.train` (commit train payload) | 없음 |

- **MicroBtbMeta는 현재 미사용** (reserved for future use) → uBTB 전용 meta 저장/소비 경로 없음

---

## 1.10 Override 및 redirection

### 우선순위 규칙

```scala
// Source: bpu/Bpu.scala:237-241, 380, 434-441
s3_flush := redirect.valid
s2_flush := s3_flush || s3_override
s1_flush := s2_flush

s3_override := s3_valid && !(s3_prediction === s3_s1Prediction)

s0_startPc := MuxCase(
  s0_startPcReg,
  Seq(
    redirect.valid -> redirect.bits.target,  // 최우선: 백엔드 redirect
    s3_override    -> s3_prediction.target,  // 2순위: mbtb s3 override
    s1_valid       -> s1_prediction.target   // 3순위: ubtb/abtb s1 prediction
  )
)
```

```scala
// Source: bpu/Bpu.scala:415-421
io.toFtq.prediction.valid := s1_valid && s2_ready || s3_override
when(s3_override) {
  io.toFtq.prediction.bits.fromStage(s3_startPc, s3_prediction)  // mbtb 결과 사용
}.otherwise {
  io.toFtq.prediction.bits.fromStage(s1_startPc, s1_prediction)  // ubtb/abtb 결과 사용
}
```

| Condition              | Winner          | Redirect Target       | Side Effect (Flush/Replay) |
|------------------------|-----------------|-----------------------|----------------------------|
| redirect.valid         | Backend redirect | redirect.bits.target | s3_flush → 전체 파이프라인 flush |
| s3_override (mbtb ≠ s1) | mbtb s3 결과   | s3_prediction.target  | s2_flush, s1_flush (s1, s2 invalid) |
| !redirect && !s3_override && s1_taken | uBTB+ABTB 후보 중 first taken(min position) | s1_prediction.target | 없음 |
| !redirect && !s3_override && !s1_taken | FallThrough | fallthrough target    | 없음 |

- ubtb miss → FallThroughPredictor가 s1 prediction 담당
- s3_override 발생 시 FTQ의 기존 s1 entry가 mbtb 결과로 갱신됨 (`s3FtqPtr` 사용)
- MicroRas (uras)가 return address 교체 가능 (s1에서 동시에 처리)
- `s1_taken`/`first taken(min position)` 선택은 BPU top의 `s1_takenMask`, `CompareMatrix`, `s1_firstTakenBranch`에서 결정
  (`bpu/Bpu.scala:266-304`)

---

## 품질 체크리스트

- [x] 1.1~1.10 순서 준수
- [x] memory depth/width/banks/read/write ports 명시
- [x] memory entry 코드 snippet 포함
- [x] field별 width/description 표 완성
- [x] pair predictor 결합 규칙 명시 (utage, uras)
- [x] pseudocode 포함
- [x] latency/throughput 수치화 (1 cycle, 1 pred/cycle)
- [x] stage 입력/출력 타이밍 명시
- [x] indexing/hash 식 + PC/history bit position 명시
- [x] training trigger/FTQ 저장/meta fields/port conflict 처리 명시
- [x] override/redirection 우선순위 및 근거 명시

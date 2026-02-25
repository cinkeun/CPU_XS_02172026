# mbtb (MainBtb) 분석

> 분석 기준: BTB_analysis_rule.md
> 분석 대상: `src/main/scala/xiangshan/frontend/bpu/mbtb/`
> Code-based only — 사전 지식/web-search 사용 금지

---

## 1.1 BTB 종류 및 역할

MainBtb는 **Region-type BTB**로, SRAM을 **32B-aligned region** 단위로 인덱싱한다.
같은 32B region 내의 모든 PC는 동일한 SRAM set에 매핑된다 — `alignOffset` (PC[4:0])은 SRAM index에서 **제외**된다.
s3에서 최종 예측 결과를 출력하며, s1 예측(ubtb/abtb)과 다를 경우 **s3_override**를 발동한다.

**Region 구조의 근거**:
- SRAM index는 PC[5] (`alignBankIdx`)부터 시작 → PC[4:0] (`alignOffset`)는 region 내부 위치에 불과
- entry의 `position` 필드 (CfiAlignedPositionWidth bits)가 region 내 branch 위치를 별도로 기록하는 이유가 여기 있음
- 코드 증거: `addrFields` 순차 레이아웃에서 `alignOffset`이 선언되지만 `getSetIndex`/`getAlignBankIndex` 등 index 추출 함수는 PC[5] 이상만 사용

**Align Banking**: 64B fetch block을 2개의 32B-aligned region으로 분할하여 각각을 별도의 alignBank가 담당.
이는 fetch block 정렬 제약을 해소하고 최대 (banks-1)/banks × predict width 커버리지를 제공한다.

```
// Source: mbtb/Parameters.scala:30-32
NumAlignBanks: Int = 2,  // FetchBlockSize(64B) / FetchBlockAlignSize(32B) = 2
// 최대 (banks-1) / banks * predict width 예측 커버 가능
```

예측 거리: **현재 block → 다음 block** (s3 출력, non-lookahead).
출력: `Vec(NumBtbResultEntries=8, Valid[Prediction])` — alignBanks×Ways = 2×4 = 8 슬롯.

| BTB  | Type   | Region Width (SRAM index 단위)    | Predict Distance          | 설명 |
|------|--------|-----------------------------------|---------------------------|------|
| mbtb | Region | FetchBlockAlignSize = 32B         | 현재 block → 다음 block (s3 출력) | 8192-entry, 2-level banking, SRAM-based |

---

## 1.2 BTB memory spec

### 구조 계층

```
MainBtb
 ├── MainBtbAlignBank[0]  (alignIdx=0)
 │    ├── MainBtbInternalBank[0] (entrySrams × NumWay, counterSram)
 │    ├── MainBtbInternalBank[1]
 │    ├── MainBtbInternalBank[2]
 │    └── MainBtbInternalBank[3]
 └── MainBtbAlignBank[1]  (alignIdx=1)
      ├── MainBtbInternalBank[0]
      ├── ...
      └── MainBtbInternalBank[3]
```

### SRAM (MainBtbInternalBank 당)

```scala
// Source: mbtb/MainBtbInternalBank.scala:90-119
// Per-way entry SRAM (NumWay=4개)
private val entrySrams = Seq.tabulate(NumWay) { wayIdx =>
  Module(new SRAMTemplate(
    new MainBtbEntry,
    set = NumSets,   // 256 (= 8192 / 4way / 4internal / 2align)
    way = 1,
    singlePort = true, shouldReset = true, holdRead = true
  ))
}
// Counter SRAM (bank당 1개, NumWay ways 공유)
private val counterSram = Module(new SRAMTemplate(
  TakenCounter(),
  set = NumSets,   // 256
  way = NumWay,    // 4
  singlePort = true, shouldReset = true, holdRead = true
))
```

### Write buffer

```scala
// Source: mbtb/MainBtbInternalBank.scala:121-133
private val entryWriteBuffer = Module(new WriteBuffer(
  new MainBtbEntrySramWriteReq,
  numEntries = WriteBufferSize,  // 4
  numPorts = NumWay              // 4 (per-way 독립 포트)
))
private val counterWriteBuffer = Module(new Queue(
  new MainBtbCounterSramWriteReq,
  WriteBufferSize,  // 4
  pipe = true, flow = true
))
```

| Memory          | Depth   | Width (bit)             | Banks                        | Read Ports | Write Ports |
|-----------------|---------|-------------------------|------------------------------|------------|-------------|
| entrySram (×4 way, ×4 internal, ×2 align = ×32 total) | 256 (NumSets) | MainBtbEntry 크기 | 1/SRAM (single-port, read priority) | 1/SRAM | 1/SRAM (write buffer 4-entry 큐잉) |
| counterSram (×4 internal, ×2 align = ×8 total)        | 256 (NumSets) | TakenCntWidth(2) × NumWay(4) = 8 bit | 1/SRAM (single-port) | 1/SRAM | 1/SRAM (Queue 4-entry 큐잉) |

- 총 물리 SRAM: entrySram 32개 + counterSram 8개 = 40개
- 총 논리 entry: NumSets(256) × NumWay(4) × NumInternalBanks(4) × NumAlignBanks(2) = **8192**

---

## 1.3 BTB memory entry 설명

```scala
// Source: mbtb/Bundles.scala:35-55
class MainBtbEntry(implicit p: Parameters) extends MainBtbBundle {
  val valid: Bool = Bool()

  val tag:       UInt            = UInt(TagWidth.W)           // 16-bit tag
  val attribute: BranchAttribute = new BranchAttribute        // 4-bit

  // Relative position to the aligned start addr
  val position: UInt = UInt(CfiAlignedPositionWidth.W)        // CfiPositionWidth - AlignBankIdxLen

  // Branch target info
  val targetCarry:     TargetCarry = new TargetCarry          // 2-bit (항상 포함)
  val targetLowerBits: UInt        = UInt(TargetWidth.W)      // 20-bit
}
```

| Field Name        | Width (bit)               | Description |
|-------------------|---------------------------|-------------|
| valid             | 1                         | entry 유효 여부 |
| tag               | 16 (TagWidth)             | PC[instOffsetBits + ... + TagWidth - 1 : ...], tag 비교용 |
| attribute         | 4                         | BranchAttribute (branchType 2-bit + rasAction 2-bit) |
| position          | CfiAlignedPositionWidth   | **region 내** branch 위치 (= CfiPositionWidth - AlignBankIdxLen). Region BTB이므로 entry가 region 내 어느 위치의 branch인지 별도 기록 필요 |
| targetCarry       | 2                         | TargetCarry (Fit/Overflow/Underflow): 항상 포함 (ubtb/abtb와 달리 optional 아님) |
| targetLowerBits   | 20 (TargetWidth)          | target 하위 비트 (2B-aligned) |

### AddrField 구조

```scala
// Source: mbtb/Helpers.scala:30-45
val addrFields = AddrField(
  Seq(
    ("alignOffset",     FetchBlockAlignWidth),  // region 내 offset (SRAM index 미사용 — Region BTB 증거)
    ("alignBankIdx",    AlignBankIdxLen),        // → SRAM index (PC[5:5])
    ("internalBankIdx", InternalBankIdxLen),     // → SRAM index (PC[7:6])
    ("setIdx",          SetIdxLen),              // → SRAM index (PC[15:8])
    ("tag",             TagWidth)                // → tag 비교용 (PC[31:16])
  ),
  extraFields = Seq(
    ("replacerSetIdx", FetchBlockSizeWidth, SetIdxLen),
    ("targetLower",    instOffsetBits, TargetWidth),
    ("position",       instOffsetBits, FetchBlockAlignWidth),
    ("cfiPosition",    instOffsetBits, FetchBlockSizeWidth)
  )
)
```

- `position` (alignBank 내 상대 위치): cfiPosition의 하위 AlignBankIdxLen 비트를 제외한 값
- s2에서 `Cat(s2_posHigherBits, e.position)`으로 full cfiPosition 복원

---

## 1.4 Pair prediction unit

```scala
// Source: bpu/Bpu.scala:322-343 (s2 conditional direction 결정)
private val s2_condTakenMask = VecInit((s2_mbtbResult zip tage.io.prediction zip s2_scUsed zip s2_scTakenMask).map {
  case (((e, p), useSc), scTaken) =>
    e.valid && e.bits.attribute.isConditional &&
    MuxCase(
      e.bits.taken,     // 기본: mbtb counter
      Seq(
        useSc         -> scTaken,          // Sc가 active: Sc 결과 사용
        p.useProvider -> p.providerPred,   // Tage provider hit: Tage 결과
        p.hasAlt      -> p.altPred         // Tage alt hit
      )
    )
})
```

```scala
// Source: bpu/Bpu.scala:356-375 (s3 target 결정)
s3_prediction.target :=
  MuxCase(
    s3_fallThroughPrediction.target,
    Seq(
      (s3_taken && s3_useRas)    -> ras.io.topRetAddr,           // RAS: return
      (s3_taken && s3_useIttage) -> ittage.io.prediction.target, // ITTage: indirect
      s3_taken                   -> s3_firstTakenBranch.bits.target  // mbtb: direct/cond
    )
  )
```

| BTB  | Paired Unit | 역할                                | 결합 방식 |
|------|-------------|-------------------------------------|-----------|
| mbtb | Tage        | 조건부 branch 방향 (provider/alt)    | mbtb entry hit → Tage.providerPred 또는 altPred 사용 |
| mbtb | Sc          | Tage override (통계적 보정)           | useSc && Sc.taken ≠ Tage.taken 시 Sc 결과 사용 |
| mbtb | ITTage      | indirect branch target 정확도 향상   | attribute.needIttage && ittage.hit → ittage.target 사용 |
| mbtb | RAS         | return address                       | attribute.isReturn && s3_taken → ras.topRetAddr 사용 |

---

## 1.5 다음 예측 pseudocode

```text
onPredict(startPc):
  // --- s0: SRAM read 요청 ---
  s0_rotator = VecRotate(getAlignBankIndex(startPc))
  for i in 0..NumAlignBanks-1:
    alignedStartPc[i] = (i==0) ? startPc : getAlignedPc(startPc + i * alignSize)
  alignBanks[rotated_idx].read(setIdx, posHigherBits, crossPage)

  // --- s1: 대기 ---
  // SRAM latency 소요

  // --- s2: 응답 수신 + 예측 출력 ---
  for each alignBank:
    tag = getTag(s2_startPc)
    for each way:
      rawHit = entry.valid && entry.tag == tag
      hit = rawHit && entry.position >= alignedInstOffset && !crossPage
      pred.valid    = hit
      pred.taken    = takenCounter[way].isPositive   // mbtb base direction
      pred.cfiPosition = Cat(posHigherBits, entry.position)
      pred.target   = getFullTarget(s2_startPc, entry.targetLowerBits, entry.targetCarry)
      meta[way] = {rawHit, position, attribute, counter}

  // multi-hit 감지 → 중복 way flush
  if detectMultiHit(hitMask, positions):
    internalBank.flush(setIdx, multiHitMask)

  // --- BPU top s2: 방향 결정 (mbtb + Tage + Sc) ---
  for each way:
    condTaken = MuxCase(mbtb.taken, [useSc->sc.taken, useProvider->tage.taken, hasAlt->tage.alt])
    jumpTaken = isDirect || isIndirect

  // --- s3: 최종 예측 선택 ---
  firstTaken = firstTaken(condTaken || jumpTaken)
  target = MuxCase(fallthrough, [isReturn->RAS, needIttage&&ittageHit->ITTage, else->mbtb.target])
  prediction = {taken, cfiPosition, target, attribute}

  // --- s3: replacer touch ---
  replacer.predictTouch(setIdx, takenMask)  // taken entry만 touch
```

---

## 1.6 Input-to-output latency 및 throughput

| BTB  | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|------|-------------|--------------|-----------------|--------------------------|
| mbtb | BPU s0      | BPU s3 (prediction valid) | 3 (s0→s1→s2→s3) | 1 (파이프라인) |

- s0: SRAM read req 발송
- s1: SRAM read resp 대기 (1-cycle SRAM latency, holdRead=true)
- s2: tag 비교, 예측 슬롯 출력 (`io.result`, `io.meta`)
- s3: replacer touch (최종 takenMask 사용)
- BPU top에서 s3_prediction이 유효해지는 시점: s3_fire

```scala
// Source: mbtb/MainBtb.scala:54-60
private val s0_fire, s1_fire, s2_fire, s3_fire = Wire(Bool())
```

---

## 1.7 Pipeline stage 위치

| Signal               | Produced @ Stage | Consumed @ Stage | Timing Note |
|----------------------|------------------|------------------|-------------|
| s0_startPcVec        | BPU s0           | mbtb s0 (alignBanks) | VecRotate 후 각 alignBank에 분배 |
| alignBank.read.resp  | mbtb s1          | mbtb s2          | `RegEnable(s1_rawEntries, s1_fire)` |
| io.result (8 slots)  | mbtb s2          | BPU s2           | `io.result := VecInit(alignBanks.flatMap(...))` |
| io.meta              | mbtb s2          | BPU s3 (s3_resolveMeta) | `RegEnable(mbtb.io.meta, s2_fire)` |
| s3_prediction        | BPU s3           | FTQ              | s3_override 결정 후 FTQ에 override 또는 s1 결과 전송 |
| io.s3_takenMask      | BPU s3 (mbtb+tage+sc 결합) | mbtb s3 (replacer) | `mbtb.io.s3_takenMask := s3_takenMask` |

```scala
// Source: mbtb/MainBtbAlignBank.scala:98-117
// s0: internalBank에 read 요청
internalBanks.zipWithIndex.foreach { case (b, i) =>
  b.io.read.req.valid       := s0_fire && s0_internalBankMask(i)
  b.io.read.req.bits.setIdx := s0_setIdx
}
// s1: Mux1H로 선택된 internalBank 결과 취득
private val s1_rawEntries  = Mux1H(s1_internalBankMask, internalBanks.map(_.io.read.resp.entries))
private val s1_rawCounters = Mux1H(s1_internalBankMask, internalBanks.map(_.io.read.resp.counters))
// s2: 래치 후 tag 비교
private val s2_rawEntries  = RegEnable(s1_rawEntries, s1_fire)
```

---

## 1.8 BTB memory indexing hashing 방법

### AddrField 레이아웃 (순차 bit 구성)

```scala
// Source: mbtb/Helpers.scala:30-45
val addrFields = AddrField(
  Seq(
    ("alignOffset",     FetchBlockAlignWidth),  // PC bit [4:0]   (log2Ceil(32)=5, FetchBlockAlignSize=32B)
    ("alignBankIdx",    AlignBankIdxLen),        // PC bit [5:5]   (log2Ceil(2)=1)
    ("internalBankIdx", InternalBankIdxLen),     // PC bit [7:6]   (log2Ceil(4)=2)
    ("setIdx",          SetIdxLen),              // PC bit [15:8]  (log2Ceil(256)=8, NumSets=256)
    ("tag",             TagWidth)                // PC bit [31:16] (TagWidth=16)
  ),
  maxWidth = Option(VAddrBits),
  extraFields = Seq(
    ("replacerSetIdx", FetchBlockSizeWidth, SetIdxLen),    // PC bit [13:6]  (FetchBlockSizeWidth=6)
    ("targetLower",    instOffsetBits, TargetWidth),       // PC bit [20:1]
    ("position",       instOffsetBits, FetchBlockAlignWidth), // PC bit [5:1]
    ("cfiPosition",    instOffsetBits, FetchBlockSizeWidth)   // PC bit [6:1]
  )
)
```

### Predict path index 계산

```scala
// Source: mbtb/Helpers.scala:47-57
def getSetIndex(pc: PrunedAddr): UInt          = addrFields.extract("setIdx",          pc)
def getAlignBankIndex(pc: PrunedAddr): UInt    = addrFields.extract("alignBankIdx",    pc)
def getInternalBankIndex(pc: PrunedAddr): UInt = addrFields.extract("internalBankIdx", pc)
def getTag(pc: PrunedAddr): UInt               = addrFields.extract("tag",             pc)
def getReplacerSetIndex(pc: PrunedAddr): UInt  = addrFields.extract("replacerSetIdx",  pc)
```

| Field | PC Bits | Width | Parameter | Note |
|-------|---------|-------|-----------|------|
| alignOffset | [4:0] | 5 | FetchBlockAlignWidth=5 | 32B 정렬 블록 내 offset, SRAM index에 미사용 |
| alignBankIdx | [5:5] | 1 | AlignBankIdxLen=1 | alignBank 선택 |
| internalBankIdx | [7:6] | 2 | InternalBankIdxLen=2 | 물리 SRAM bank 선택 |
| setIdx | [15:8] | 8 | SetIdxLen=8 | SRAM row 선택 (NumSets=256) |
| tag | [31:16] | 16 | TagWidth=16 | entry 비교 |
| replacerSetIdx | [13:6] | 8 | SetIdxLen=8 | PLRU 상태 SRAM 인덱스 (setIdx와 시작 bit 상이) |

**replacerSetIdx 차이**: 일반 `setIdx` = PC[15:8]이지만 `replacerSetIdx` = PC[13:6].
`FetchBlockSizeWidth=6`(bit 6)에서 시작 — PC[5:0]의 alignOffset + alignBankIdx를 건너뛰고 alignBank 경계 상위에서 시작한다.

### AlignBank 분배: VecRotate

```scala
// Source: mbtb/MainBtb.scala (predict 섹션)
// s0_startPcVec를 VecRotate로 회전하여 각 물리 alignBank가
// alignBankIdx == i인 PC를 수신하도록 분배
```

- history 사용: **없음**
- hash 없음 (XOR/fold 미적용), PC 단순 bit extraction

### Train path: cfiPosition → alignBankIdx

```scala
// Source: mbtb/Helpers.scala:56-57
def getAlignBankIndexFromPosition(cfiPosition: UInt): UInt =
  addrFields.extractFrom("cfiPosition", "alignBankIdx", cfiPosition)
// cfiPosition = PC[6:1] (FetchBlockSizeWidth=6 bits, instOffsetBits=1)
// alignBankIdx within cfiPosition = bit[4] of cfiPosition (= PC[5])
```

| Path | Field | 소스 | PC Bits | History | Note |
|------|-------|------|---------|---------|------|
| Predict | alignBankIdx | s0_startPc[5:5] | [5:5] | 없음 | VecRotate로 alignBank에 분배 |
| Predict | internalBankIdx | s0_startPc[7:6] | [7:6] | 없음 | |
| Predict | setIdx | s0_startPc[15:8] | [15:8] | 없음 | |
| Predict | replacerSetIdx | s0_startPc[13:6] | [13:6] | 없음 | PLRU 전용 |
| Predict | tag | s0_startPc[31:16] | [31:16] | 없음 | |
| Train | alignBankIdx | getAlignBankIndexFromPosition(cfiPosition) | [5:5] via cfiPos bit 4 | 없음 | mispredict branch 위치에서 추출 |
| Train | setIdx | mbtbMeta 복원 | — | 없음 | predict 시점 값 재사용 |
| Train | tag | getTag(t1_train.startPc) | [31:16] | 없음 | |

---

## 1.9 Training 방법

### Trigger: mispredict 기반 (commit 시점, FTQ → BPU train)

```scala
// Source: mbtb/MainBtb.scala:124-151
private val t0_fire  = io.stageCtrl.t0_fire && io.enable
private val t0_train = io.train    // FTQ에서 온 BpuTrain

// t1: write 대상 alignBank 결정
private val t1_writeAlignBankIdx  = getAlignBankIndexFromPosition(t1_mispredictInfo.bits.cfiPosition)
private val t1_writeAlignBankMask = t1_rotator.rotate(VecInit(UIntToOH(t1_writeAlignBankIdx).asBools))

alignBanks.zipWithIndex.foreach { case (b, i) =>
  b.io.write.req.valid         := t1_fire && t1_writeAlignBankMask(i)
  b.io.write.req.bits.mispredictInfo := t1_mispredictInfo
}
```

```scala
// Source: mbtb/MainBtbAlignBank.scala:213-222 (entry write 조건)
private val t1_entryNeedWrite = t1_mispredictInfo.valid && (
  !t1_hit ||                                            // 1. miss: 새 entry 할당
  t1_mispredictInfo.bits.attribute.needIttage ||        // 2. indirect: target 갱신
  !(t1_mispredictInfo.bits.attribute === Mux1H(t1_hitMask, t1_meta.map(_.attribute)))  // 3. attribute 변경
)
```

### FTQ meta 저장 (MainBtbMeta)

```scala
// Source: mbtb/Bundles.scala:69-80
class MainBtbMetaEntry(implicit p: Parameters) extends MainBtbBundle {
  val rawHit:    Bool            = Bool()
  val position:  UInt            = UInt(CfiPositionWidth.W)
  val attribute: BranchAttribute = new BranchAttribute
  val counter:   SaturateCounter = TakenCounter()     // 2-bit 포화 카운터

  def hit(branch: BranchInfo): Bool = rawHit && position === branch.cfiPosition
}
class MainBtbMeta(implicit p: Parameters) extends MainBtbBundle {
  val entries: Vec[Vec[MainBtbMetaEntry]] = Vec(NumAlignBanks, Vec(NumWay, new MainBtbMetaEntry))
}
```

### Counter 갱신 (t1)

```scala
// Source: mbtb/MainBtbAlignBank.scala:252-263
t1_meta.zipWithIndex.foreach { case (meta, i) =>
  val hitMask    = t1_branches.map { b =>
    b.valid && b.bits.attribute.isConditional && meta.position === b.bits.cfiPosition
  }
  val actualTaken = Mux1H(hitMask, t1_branches.map(_.bits.taken))
  val entryOverridden = t1_entryNeedWrite && t1_entryWayMask(i)

  t1_newCounters(i) := Mux(entryOverridden,
    TakenCounter.WeakPositive,          // 새 entry 할당 시 WeakPositive 초기화
    meta.counter.getUpdate(actualTaken) // 기존 entry: actualTaken에 따라 증감
  )
}
// counter는 mispredict 여부 무관하게 모든 resolved branch에 대해 갱신
```

| Trigger                           | Required FTQ Info                                  | FTQ Storage                  | Write Port / Conflict Handling |
|-----------------------------------|----------------------------------------------------|------------------------------|--------------------------------|
| t0_fire (FTQ commit → BPU train) | mispredictBranch (valid, position, target, attribute), meta.mbtb (rawHit, position, counter), branches (all resolved) | MainBtbMeta (FTQ resolveMeta에 저장) | entry: write buffer (4-entry, per-way 포트), counter: Queue (4-entry, drop on full) |

- entry write buffer full 시 write 요청 drop (`entry_writebuffer_drop_write` perf counter)
- counter write buffer (Queue) full 시 drop (`counter_writebuffer_drop_write` perf counter)
- flush (multi-hit 처리)와 entry write 같은 setIdx 충돌 시 priority 처리:

```scala
// Source: mbtb/MainBtbInternalBank.scala:168-176
val conflict = writeEntry.req.valid &&
  writeEntry.req.bits.setIdx === flush.req.bits.setIdx &&
  writeEntry.req.bits.entry.tag === 0.U
// conflict 시 flush를 skip, write 우선
```

---

## 1.10 Override 및 redirection

### s3_override 발생 조건

```scala
// Source: bpu/Bpu.scala:380
s3_override := s3_valid && !(s3_prediction === s3_s1Prediction)
// s3_prediction: mbtb + Tage + Sc + ITTage + RAS 결합 최종 예측
// s3_s1Prediction: 당시 s1 예측 (ubtb/abtb 기반)
```

### Flush 전파

```scala
// Source: bpu/Bpu.scala:237-239
s3_flush := redirect.valid
s2_flush := s3_flush || s3_override   // s3_override 시 s1/s2 flush
s1_flush := s2_flush
```

### nextPC 선택

```scala
// Source: bpu/Bpu.scala:434-441
s0_startPc := MuxCase(
  s0_startPcReg,
  Seq(
    redirect.valid -> redirect.bits.target,    // 최우선: backend redirect
    s3_override    -> s3_prediction.target,    // 2순위: mbtb s3 override
    s1_valid       -> s1_prediction.target     // 3순위: ubtb/abtb s1 결과
  )
)
```

### FTQ override 처리

```scala
// Source: bpu/Bpu.scala:415-421
io.toFtq.prediction.valid := s1_valid && s2_ready || s3_override
when(s3_override) {
  io.toFtq.prediction.bits.fromStage(s3_startPc, s3_prediction)
  // s3FtqPtr로 FTQ의 기존 s1 entry를 mbtb 결과로 덮어씀
}
```

### Replacer 갱신 (s3)

```scala
// Source: mbtb/MainBtbAlignBank.scala:190-193
// taken entry만 touch: not-taken conditional은 덜 유용하므로 먼저 victim 대상
replacer.io.predictTouch.valid        := s3_fire && s3_takenMask.reduce(_ || _)
replacer.io.predictTouch.bits.setIdx  := s3_replacerSetIdx
replacer.io.predictTouch.bits.wayMask := s3_takenMask.asUInt
// s3_takenMask = mbtb + Tage + Sc 결합 최종 방향 (not just mbtb counter)
```

| Condition                          | Winner          | Redirect Target             | Side Effect (Flush/Replay) |
|------------------------------------|-----------------|-----------------------------|----------------------------|
| redirect.valid (backend)           | Backend         | redirect.bits.target        | s3_flush → 전체 파이프라인 flush, replacer 갱신 없음 |
| s3_override (s3 pred ≠ s1 pred)    | mbtb s3 결과    | s3_prediction.target        | s2_flush, s1_flush; FTQ entry 덮어쓰기 (s3FtqPtr 사용) |
| mbtb hit, no override              | mbtb (fallback to s1) | s1_prediction.target  | 없음 (s3 meta만 FTQ에 저장) |
| mbtb miss (all ways invalid)       | FallThrough     | fallthrough target          | 없음 |
| indirect + ittage hit              | mbtb + ITTage   | ittage.prediction.target    | target만 교체, position/attribute는 mbtb 유지 |
| return + RAS valid                 | mbtb + RAS      | ras.topRetAddr              | target만 교체 |

---

## 품질 체크리스트

- [x] 1.1~1.10 순서 준수
- [x] memory depth/width/banks/read/write ports 명시
- [x] memory entry 코드 snippet 포함
- [x] field별 width/description 표 완성
- [x] pair predictor 결합 규칙 명시 (Tage, Sc, ITTage, RAS)
- [x] pseudocode 포함
- [x] latency/throughput 수치화 (3 cycle, BPU s3 출력)
- [x] stage 입력/출력 타이밍 명시
- [x] indexing/hash 식 + PC/history bit position 명시
- [x] training trigger/FTQ 저장/meta fields/port conflict 처리 명시
- [x] override/redirection 우선순위 및 근거 명시

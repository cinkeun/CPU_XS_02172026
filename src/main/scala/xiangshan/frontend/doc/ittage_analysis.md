# ittage (ITTage) 분석

> 분석 기준: prediction_analysis_rule.md
> 분석 대상: `src/main/scala/xiangshan/frontend/bpu/ittage/`
> Code-based only — 사전 지식/web-search 사용 금지

---

## 1.1 prediction unit 종류 및 역할

ITTage는 **Indirect Target TAGE** predictor로, `needIttage` attribute를 가진 indirect branch의 **target address**를 예측한다.
TAGE 방식의 다중 tagged table + provider/alt-provider 선택 구조를 사용하며, table entry에는 full target을 넣지 않고
`targetOffset(offset + pointer + usePcRegion)`만 저장한다.  
여기서 `pointer`는 별도의 `RegionWays` register file(16-entry)에서 target 상위 비트(region)를 가리키는 인덱스다.

- CFI 처리 단위: 1개 (한 번의 예측에 하나의 indirect branch target)
- 예측 거리: **현재 block → 다음 block** (non-lookahead, BPU s3 출력)
- 기여 방식: mbtb가 탐지한 indirect branch의 target을 BPU s3에서 교체

```scala
// Source: bpu/Bpu.scala:356
private val s3_useIttage = s3_firstTakenBranch.bits.attribute.needIttage && ittage.io.prediction.hit
```

| Unit   | Type             | CFI per Entry | Predict Distance         | 설명 |
|--------|------------------|---------------|--------------------------|------|
| ITTage | Indirect-target TAGE | 1 (indirect branch) | 현재 block → 다음 block (s3 출력) | 5-table tagged, provider/alt, RegionWays target compression |

---

## 1.2 prediction unit memory spec

### Tagged Tables (5개)

```scala
// Source: ittage/Parameters.scala:23-29
TableInfos: Seq[IttageTableInfo] = Seq(
  new IttageTableInfo(256,  4),   // T0
  new IttageTableInfo(256,  8),   // T1
  new IttageTableInfo(512, 13),   // T2
  new IttageTableInfo(512, 16),   // T3
  new IttageTableInfo(512, 32)    // T4
)
NumBanks:           Int = 2
TagWidth:           Int = 9
ConfidenceCntWidth: Int = 2
UsefulCntWidth:     Int = 1
TargetWidth:        Int = 20     // 2B-aligned
TableSramSize:      Int = 128    // physical SRAM depth per fold
TableWriteBufferSize: Int = 4
```

| Table | Total Rows | HistLen | Banks | Rows/Bank | Physical SRAM depth | Entry Width (bit) | Read Ports | Write Ports |
|-------|-----------|---------|-------|-----------|---------------------|-------------------|------------|-------------|
| T0 | 256 | 4 | 2 | 128 | 128 (foldedWidth=1) | 38 | 1/bank | 1/bank (WriteBuffer 4-entry) |
| T1 | 256 | 8 | 2 | 128 | 128 | 38 | 1/bank | 1/bank |
| T2 | 512 | 13 | 2 | 256 | 128 (foldedWidth=2) | 38 | 1/bank | 1/bank |
| T3 | 512 | 16 | 2 | 256 | 128 | 38 | 1/bank | 1/bank |
| T4 | 512 | 32 | 2 | 256 | 128 | 38 | 1/bank | 1/bank |

- 총 물리 SRAM: 5 tables × 2 banks = **10개** FoldedSRAMTemplate
- Entry width: `1(valid) + 9(tag) + 2(confidenceCnt) + 1(usefulCnt) + 20(offset) + 4(pointer) + 1(usePcRegion) = 38 bits + 1(paddingBit)`
- banking 목적: predict(read)와 update(write)가 다른 bank를 사용하여 port conflict 회피

### RegionWays (target region 압축)

```scala
// Source: ittage/Parameters.scala:41-43
RegionNums:     Int = 16
RegionPorts:    Int = 2
RegionReplacer: String = "plru"
// Source: ittage/Parameters.scala:74
def RegionBits: Int = VAddrBits - TargetOffsetWidth  // 상위 비트 폭
```

| Memory | Depth | Width (bit) | Type | Read Ports | Write Ports | Replacer |
|--------|-------|------------|------|------------|-------------|----------|
| RegionWays | 16 | RegionBits (= VAddrBits - 20) | Register (VecInit) | 5 (predict, pointer-indexed valid-bit) + 2 (update-search, by region content) | 1 (per cycle) | PLRU (write-only touch) |

압축 방식의 정확한 의미는 아래와 같다.

- `RegionWays`는 "전체 주소공간 테이블"이 아니라, 최근/빈번 target region만 담는 **16-entry region dictionary**다.
- 각 ITTage table entry는 target 상위 비트 전체를 저장하지 않고, `pointer(4bit)`로 dictionary entry를 참조한다.
- 여러 ITTage entry가 같은 region을 가리키면 같은 pointer를 공유하므로 상위 비트 중복 저장을 줄인다.
- 복원 시 `respHit(pointer) && !usePcRegion`이면 `Cat(regionWays[pointer], offset)`를 쓰고, 아니면 `Cat(PC_region, offset)`로 fallback한다.

> **[주의] predict read `respHit`는 content match가 아닌 valid-bit 확인이다**
> ```scala
> // RegionWays.scala:66-67
> io.respHit(i)    := regions(io.reqPointer(i)).valid   // ← pointer 위치 valid bit만 확인
> io.respRegion(i) := regions(io.reqPointer(i)).region
> ```
> PLRU가 해당 entry를 evict하고 다른 region으로 overwrite한 뒤에도, 기존 pointer를 가진 ITTage entry는
> `respHit=true`를 받아 **wrong region**으로 target을 복원할 수 있다.
> (misprediction은 FTQ commit 시 수정됨)

> **[주의] PLRU는 write 시에만 갱신된다 (predict read는 PLRU 상태 불변)**
> ```scala
> // RegionWays.scala:88-90
> replacerTouchWays(0).valid := io.writeValid   // ← io.writeValid일 때만 touch
> replacerTouchWays(0).bits  := writePointer
> replacer.access(replacerTouchWays)
> ```
> predict read(`reqPointer` 경로)는 PLRU를 건드리지 않는다.
> replacement는 train(write) 시점의 접근 패턴만 반영한다.

> **[참고] update-search ports (RegionPorts=2, train 경로 전용)**
> ```scala
> // RegionWays.scala:70-79
> val updateTotalHits =
>   VecInit((0 until RegionNums).map(w => regions(w).region === io.updateRegion(i) && regions(w).valid))
> val updateBypass  = (io.updateRegion(i) === io.writeRegion) && io.writeValid
> val updateHit     = updateTotalHits.reduce(_ || _) || updateBypass
> val updatePointer = Mux(updateBypass, writePointer, OHToUInt(updateTotalHits))
> ```
> predict read(pointer-indexed)와 달리, train 경로에서 provider/altProvider의 region pointer를
> **region 값으로 content-search**하는 포트 2개(`RegionPorts=2`).
> write와 동시에 같은 region이 쓰이는 경우를 위한 bypass 로직 포함.

간단 예시 (개념 예시):

- target = `0x0000_1234_5678`
- 분해: `region = target[VAddrBits-1:20]`, `offset = target[19:0]`
- ITTage entry 저장값: `offset=0x5678`, `pointer=3`, `usePcRegion=0`
- RegionWays[3] 저장값: `region=0x0000_1234`
- 예측 시 복원: `Cat(RegionWays[3], 0x5678) = 0x0000_1234_5678`

---

## 1.3 prediction unit memory entry 설명

### IttageEntry (tagged table 저장 단위)

```scala
// Source: ittage/Bundles.scala:42-55
class IttageEntry(tagLen: Int)(implicit p: Parameters) extends IttageBundle {
  val valid:         Bool            = Bool()
  val tag:           UInt            = UInt(tagLen.W)
  val confidenceCnt: SaturateCounter = ConfidenceCounter()  // 2-bit
  val targetOffset:  IttageOffset    = new IttageOffset()
  val usefulCnt:  SaturateCounter = UsefulCounter()         // 1-bit (최하위 — bitmask 갱신 목적)
  val paddingBit: UInt            = UInt(1.W)
}

// Source: ittage/Bundles.scala:51-55
class IttageOffset(implicit p: Parameters) extends IttageBundle {
  val offset:      PrunedAddr = PrunedAddr(TargetOffsetWidth)  // target[TargetOffsetWidth-1:0], ≈19bit 유효
  val pointer:     UInt       = UInt(log2Ceil(RegionNums).W)   // 4-bit → RegionWays 인덱스
  val usePcRegion: Bool       = Bool()                         // true: PC의 region으로 상위 비트 대체
}
```

| Field Name | Width (bit) | Description |
|------------|-------------|-------------|
| valid | 1 | entry 유효 여부 |
| tag | 9 (TagWidth) | PHR folded history 기반 해시 tag |
| confidenceCnt | 2 (ConfidenceCntWidth) | target 정확도 신뢰도 카운터 (correct→up, mispred→down) |
| targetOffset.offset | ≈19 (PrunedAddr(20)) | target 하위 20-bit (2B-aligned, 최하위 alignment bit 제거) |
| targetOffset.pointer | 4 (log2Ceil(16)) | RegionWays 16-entry dictionary 인덱스 (target 상위 비트 직접 저장 대신 간접 참조) |
| targetOffset.usePcRegion | 1 | true면 RegionWays를 보지 않고 PC region으로 상위 비트를 구성. false라도 rTable miss면 PC region fallback |
| usefulCnt | 1 (UsefulCntWidth) | TAGE useful bit (allocation 시 0으로 리셋; periodically reset) |
| paddingBit | 1 | DontCare (bit-alignment 용도) |

> entry 크기 sanity check: `require(ittageEntrySz == (new IttageEntry(tagLen)).getWidth)`
> `ittageEntrySz = 1 + tagLen + ConfidenceCntWidth + UsefulCntWidth + TargetOffsetWidth + log2Ceil(RegionNums) + 1 = 38`

### RegionEntry (RegionWays 저장 단위)

```scala
// Source: ittage/RegionWays.scala:42-45
private class RegionEntry(implicit p: Parameters) extends IttageBundle {
  val valid:  Bool = Bool()
  val region: UInt = UInt(RegionBits.W)  // target 상위 비트 (= VAddrBits - TargetOffsetWidth bits)
}
```

| Field Name | Width (bit) | Description |
|------------|-------------|-------------|
| valid | 1 | entry 유효 여부 |
| region | RegionBits (= VAddrBits - 20) | target 주소 상위 비트 (대부분의 indirect jump는 동일 region 내에서 발생) |

### IttageMeta (FTQ 저장)

```scala
// Source: ittage/Bundles.scala:62-80
class IttageMeta(implicit p: Parameters) extends IttageBundle {
  val valid:             Bool            = Bool()
  val provider:          Valid[UInt]     = Valid(UInt(log2Ceil(NumTables).W))  // 3-bit
  val altProvider:       Valid[UInt]     = Valid(UInt(log2Ceil(NumTables).W))
  val altDiffers:        Bool            = Bool()
  val providerUsefulCnt: SaturateCounter = UsefulCounter()
  val providerCnt:       SaturateCounter = ConfidenceCounter()
  val altProviderCnt:    SaturateCounter = ConfidenceCounter()
  val allocate:          Valid[UInt]     = Valid(UInt(log2Ceil(NumTables).W))
  val providerTarget:    PrunedAddr      = PrunedAddr(VAddrBits)
  val altProviderTarget: PrunedAddr      = PrunedAddr(VAddrBits)
}
```

---

## 1.4 pair BTB unit 설명

ITTage는 **mbtb**와 짝을 이룬다.
mbtb가 entry의 `attribute.needIttage` 플래그를 통해 indirect branch를 탐지하고, ITTage가 해당 branch의 정확한 target을 제공한다.

```scala
// Source: bpu/Bpu.scala:356, 367-374
private val s3_useIttage = s3_firstTakenBranch.bits.attribute.needIttage && ittage.io.prediction.hit

s3_prediction.target :=
  MuxCase(
    s3_fallThroughPrediction.target,
    Seq(
      (s3_taken && s3_useRas)    -> ras.io.topRetAddr,
      (s3_taken && s3_useIttage) -> ittage.io.prediction.target,  // ← ITTage target 교체
      s3_taken                   -> s3_firstTakenBranch.bits.target
    )
  )
```

| Prediction Unit | Paired BTB | Pairing Purpose | 결합 Stage/Signal |
|-----------------|------------|-----------------|-------------------|
| ITTage | mbtb | indirect branch target 정밀화 | BPU s3 / `s3_useIttage = needIttage && ittage.hit` |

- mbtb는 position, attribute, targetCarry를 제공
- ITTage는 target 전체 주소를 교체 (position/attribute는 mbtb 그대로 사용)

---

## 1.5 다음 예측 pseudocode (paired mbtb 포함)

```text
onPredict(s0_startPc, s1_startPc, s1_foldedPhr, s2_mbtbResult):

  // --- BPU s1: 5개 table에 SRAM read request ---
  for each table T in T0..T4:
    unhashedIdx = s1_startPc >> instOffsetBits
    (bankIdx, setIdx, tag) = computeTagAndHash(unhashedIdx, s1_foldedPhr)
      // bankIdx = unhashedIdx[bankIdxWidth-1:0]
      // setIdx  = (unhashedIdx[bankIdxWidth+setIdxWidth-1:bankIdxWidth] XOR idxFh)[setIdxWidth-1:0]
      // tag     = (unhashedIdx>>idxFullWidth XOR tagFh XOR altTagFh<<1)[tagLen-1:0]
    T.sram[bankIdx].read(setIdx)

  // --- BPU s2: resp + provider 선택 + region 조회 ---
  for each table T:
    entry = T.sram_resp[s1_bankIdx]
    hit   = entry.valid && entry.tag == s1_tag
    T.resp = (hit, entry.confidenceCnt, entry.usefulCnt, entry.targetOffset)

  // ParallelSelectTwo: hit한 table 중 가장 긴 history 2개 선택
  provider    = last-index hit table  // longest history
  altProvider = second hit table

  // RegionWays 조회: pointer → region 상위 bits 복원
  for each table T:
    if rTable.respHit(T.pointer) && !T.usePcRegion:
      regionTarget(T) = Cat(rTable.respRegion(T.pointer), T.offset)
    else:
      regionTarget(T) = Cat(targetGetRegion(s2_startPc), T.offset)  // PC region 사용

  // target 선택: provider 신뢰도에 따라 alt 사용 여부 결정
  providerNull = provider.confidenceCnt.isSaturateNegative
  ittageTarget = if (provided && !providerNull): providerTarget
                 elif (providerNull && altProvided): altProviderTarget
                 else: 0 (no valid target)

  s2_provided    = provided
  s2_ittageTarget = ittageTarget

  // --- BPU s3: 출력 ---
  io.prediction.hit    = s3_fire && s3_provided
  io.prediction.target = s3_ittageTarget

  // --- BPU top s3: mbtb와 결합 ---
  if mbtb.firstTakenBranch.attribute.needIttage && ittage.prediction.hit:
    finalTarget = ittage.prediction.target   // mbtb target 교체
  else if mbtb.firstTakenBranch.attribute.isReturn && s3_taken:
    finalTarget = ras.topRetAddr
  else:
    finalTarget = mbtb.target

  // meta 출력 (FTQ 저장)
  ittageMeta = {valid, provider, altProvider, altDiffers,
                providerCnt, altProviderCnt, providerUsefulCnt,
                allocate, providerTarget, altProviderTarget}
```

---

## 1.6 Input-to-output latency 및 throughput

```scala
// Source: ittage/IttageTable.scala:129-131
// Table 내부: s0(req) → s1(resp), 1-cycle SRAM latency
private val (s1_setIdx, s1_tag) = (RegEnable(s0_setIdx, io.req.fire), RegEnable(s0_tag, io.req.fire))
private val s1_valid            = RegNext(s0_valid)

// Source: ittage/Ittage.scala:102-111
// Ittage top: s2(table resp wire) → s3(output reg)
private val s3_ittageTarget = RegEnable(s2_ittageTarget, s2_fire)
// io.prediction.hit/target valid at s3_fire
```

| Unit | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|------|-------------|--------------|-----------------|--------------------------|
| ITTage | BPU s1 (SRAM req) | BPU s3 (prediction) | 2 (s1→s2→s3) | 1 |

- s1: SRAM read request 발송
- s2: SRAM resp + tag compare + provider 선택 + region 조회 + target 조립
- s3: io.prediction.hit/target valid

---

## 1.7 Pipeline stage 위치

```scala
// Source: ittage/Ittage.scala:60-67
private val s0_startPc = io.startPc
private val s1_startPc = RegEnable(s0_startPc, s0_fire)  // BPU s0→s1 래치
private val s2_startPc = RegEnable(s1_startPc, s1_fire)  // BPU s1→s2 래치

// Source: ittage/Ittage.scala:190-193
tables.foreach { t =>
  t.io.req.valid           := s1_fire && s1_isIndirect
  t.io.req.bits.startPc    := s1_startPc
  t.io.req.bits.foldedHist := io.s1_foldedPhr      // ← BPU s1에서 PHR folded history 입력
}

// Source: ittage/Ittage.scala:89
private val s2_resps = VecInit(tables.map(t => t.io.resp))  // table resp는 BPU s2에서 유효

// Source: ittage/Ittage.scala:102
private val s3_ittageTarget = RegEnable(s2_ittageTarget, s2_fire)  // s2→s3 래치
```

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
|--------|------------------|------------------|-------------|
| t.io.req (SRAM read req) | BPU s1 | IttageTable 내부 s0 | s1_startPc + s1_foldedPhr |
| t.io.resp (SRAM resp + tag hit) | IttageTable 내부 s1 (= BPU s2) | Ittage s2 | RegNext(req.fire) 내부 래치 |
| s2_ittageTarget, s2_provided | BPU s2 | — | wire: ParallelSelectTwo + RegionWays lookup |
| io.prediction.hit / target | BPU s3 | BPU top s3 | RegEnable(s2_, s2_fire) |
| io.meta (IttageMeta) | BPU s3 | FTQ (resolveMeta) | ittageMeta := ... at s3_fire |

---

## 1.8 Training 방법

### Trigger 및 branch 선택

```scala
// Source: ittage/Ittage.scala:116-167
private val t0_fire    = io.enable && io.stageCtrl.t0_fire  // FTQ commit (mbtb와 동일한 train trigger)
private val t1_train   = RegEnable(io.train, ..., t0_fire)  // t0→t1 1-cycle 래치

// train 대상 branch: needIttage && taken 인 것 하나만
val trainBranchIdxVec = VecInit(t1_train.branches.map(b =>
  b.valid && b.bits.attribute.needIttage && b.bits.taken
))
val hasTrainBranch = trainBranchIdxVec.asUInt.orR
assert(PopCount(trainBranchIdxVec) <= 1.U)  // 최대 1개

private val updateValid   = hasTrainBranch && RegNext(t0_fire, false.B)
private val updateMisPred = hasTrainBranch && t1_train.branches(trainBranchIdx).bits.mispredict
```

### Update 로직 (3가지 경우)

```scala
// Source: ittage/Ittage.scala:336-398

// Case 1: Provider 갱신 (updateValid && provider.valid → 항상 수행)
updateMask(provider)          := true.B
updateCorrect(provider)       := providerTarget == realTarget
updateOldCnt(provider)        := t1_meta.providerCnt       // confidenceCnt 갱신
updateUsefulCnt(provider)     := if !altDiffers: keep
                                  else: (providerTarget == realTarget).asUInt  // 1-bit

// Case 2: Alt-provider 패널티 (usedAltPred && updateMisPred 시)
// → altProvider의 confidenceCnt 감소 (correct=false)
updateMask(altProvider) := true.B; updateCorrect(altProvider) := false.B

// Case 3: 새 entry 할당 (updateMisPred && !(providerCorrect && providerUnconf) 시)
when(allocate.valid) {
  updateMask(allocate.bits) := true.B
  updateAlloc(allocate.bits) := true.B       // → confidenceCnt = WeakPositive으로 초기화
  updateUsefulCnt(allocate.bits) := UsefulCounter.SaturateNegative  // useful=0
  updateTargetOffset(allocate.bits) := updateRealTargetOffset
}
// 할당 불가(usefulCnt 전부 1) → tickCnt 증가; tickCnt saturate → 전체 usefulCnt reset
tickCnt.selfUpdate(!allocate.valid)
when(tickCnt.isSaturatePositive) { updateResetUsefulCnt := true.B }
```

### target 인코딩 (RegionWays write)

```scala
// Source: ittage/Ittage.scala:307-319
private val updatePCRegion         = targetGetRegion(t1_train.startPc)  // PC 상위 bits
private val updateRealTargetRegion = targetGetRegion(updateRealTarget)   // target 상위 bits

updateRealTargetOffset.usePcRegion := updateRealUsePCRegion || !updateAlloc.reduce(_ || _)
rTable.io.writeValid               := !updateRealUsePCRegion && updateAlloc.reduce(_ || _)
rTable.io.writeRegion              := updateRealTargetRegion
updateRealTargetOffset.pointer     := rTable.io.writePointer
// PCRegion: target region == PC region → region bits 저장 불필요 (usePcRegion=true로 표시)
```

- `writePointer`는 RegionWays 내부에서 "`writeRegion`과 동일한 region이 이미 있으면 그 index, 없으면 invalid slot 또는 PLRU victim"으로 결정된다.
- 즉 질문하신 해석처럼, ITTage entry는 "region 자체"가 아니라 "region dictionary index(pointer)"를 저장하는 방식이 맞다.

| Trigger | Required FTQ Info | FTQ Storage | Write Port / Conflict Handling |
|---------|------------------|-------------|--------------------------------|
| t0_fire (FTQ commit, mispredict-based) | mispredictBranch (needIttage, taken, target, mispredict), IttageMeta (provider, altProvider, providerTarget, altProviderTarget, providerCnt, altDiffers, allocate) | IttageMeta (FTQ resolveMeta, io.train.meta.ittage) | 각 table: per-bank WriteBuffer(4-entry); write 요청 시 해당 bank 유휴 cycle에 SRAM 기록. update_drop perf counter 있음 |

- `usefulCnt` 전용 periodic reset: `usefulCanReset` 조건에서 1 set/cycle씩 sweep (Counter 기반)
- write 시 read와 같은 bank 충돌 → read priority (write는 buffer에 대기)

---

## 품질 체크리스트

- [x] 1.1~1.8 순서 준수
- [x] memory depth/width/tables/banks/read/write ports 명시
- [x] memory entry 코드 snippet 포함
- [x] field별 width/description 표 완성
- [x] pair BTB (mbtb) 결합 규칙 명시
- [x] paired BTB 포함 pseudocode 작성
- [x] latency/throughput 수치화 (2 cycle, BPU s3 출력)
- [x] stage 입력/출력 타이밍 명시
- [x] training trigger/FTQ 저장/meta fields/port conflict 처리 명시

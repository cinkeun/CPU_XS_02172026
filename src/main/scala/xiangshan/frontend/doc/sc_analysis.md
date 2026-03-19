# SC (Statistical Corrector) analysis

> Analysis-based: Scala/Chisel code only (code-based)
> Analysis target: `src/main/scala/xiangshan/frontend/bpu/sc/`

---

## 1.0 Why SC is needed

Bottom line: **Even if the TAGE provider points in the "right direction", the opposite is statistically more likely to be true in certain (PC, history) contexts.**

### Structural limitations of TAGE provider

The final prediction of TAGE is the saturating counter sign in the provider table. When the counter is in `weakTaken(+1)` or `weakNotTaken(-1)`, prediction reliability is low. Even if the counter is saturated, it may be systematically wrong under certain path/history patterns.

### Inverse relationship between TAGE confidence and SC intervention threshold

```scala
// Source: sc/Sc.scala:318-334
// s2_providerCtr = io.providerTakenCtrs.map(_.bits)  ← TAGE provider의 takenCtr (prediction counter)
// useful counter가 아님: tageConf는 "얼마나 강하게 taken/not-taken을 예측하는가"를 나타냄
val tageConfHigh = s2_providerCtr(i).isSaturatePositive || s2_providerCtr(i).isSaturateNegative
val tageConfMid  = s2_providerCtr(i).isMid
val tageConfLow  = s2_providerCtr(i).isWeak

when(hit && valid && tageConfHigh) {
conf := aboveThreshold(sum, thres >> 1) // threshold ÷ 2 → difficult to intervene
}.elsewhen(hit && valid && tageConfMid) {
  conf := aboveThreshold(sum, thres >> 2)   // threshold ÷ 4
}.elsewhen(hit && valid && tageConfLow) {
conf := aboveThreshold(sum, thres >> 3) // threshold ÷ 8 → Easy intervention
}
```

**`tageConf`는 TAGE provider의 `takenCtr` (prediction counter) 기반이다.** useful counter가 아님.
- `takenCtr`는 해당 entry가 방향을 얼마나 확신하는지를 나타냄 (saturated = 강한 확신, weak = 불확실)
- useful counter는 "이 entry가 alt보다 맞았는가"를 나타내므로 confidence 판단에 사용되지 않음

The weaker the TAGE (uncertainty), the lower the SC threshold, making it easier to override. If the TAGE is saturating (highly confident), the higher the threshold value, making it difficult for the SC to intervene.

### Types of biases SC learns

| table | index configuration | Capturing Bias | Activate |
|---|---|---|---|
| `biasTable` | `(PC, tageProviderIsWeak, tageProviderTaken)` | TAGE's own weak prediction direction bias | **true** |
| `pathTable` | `(PC, path history)` | Branch bias according to call path | **true** |
| `globalTable` | `(PC, GHR)` | Global branching pattern bias | false |
| `bwTable` | `(PC, backward history)` | Loop/reverse pattern bias | false |

The key is that the `biasTable` index includes `tageProviderIsWeak` and `tageProviderTaken`:

```scala
// Source: sc/Sc.scala:287-293
private val s2_biasIdxLowBits = VecInit(s2_providerTakenMask.zip(s2_providerValid).zip(s2_providerCtr).map {
  case ((taken, valid), ctr) => Cat(valid && ctr.isWeak, valid && taken)
})
// biasIdx = Cat(wayIdx, providerIsWeak, providerTaken)
// Directly learn “how often when TAGE predicts weakly taken, it is actually not taken”
```

### Self-calibration threshold

```scala
// Source: sc/Sc.scala:452-456
val scWrong = taken =/= t1_meta.scPred(branchIdx)
val shouldUpdate = writeValid && ...
(t1_meta.tagePred(branchIdx) =/= t1_meta.scPred(branchIdx)) && // only when SC is different from TAGE
  (scWrong || !t1_meta.sumAboveThres(branchIdx))
prevThres.getUpdate(scWrong, en = shouldUpdate)
// scWrong=true  → threshold 증가 (SC가 틀렸으니 더 높은 sum이 필요)
// scWrong=false → threshold 감소 (SC가 맞지만 sum < threshold → bar 낮춰 더 자주 개입)
```

**`scThreshold`는 총 8개 레지스터 (per way-slot)**
```scala
// Source: sc/Sc.scala:78
val scThreshold = RegInit(VecInit.tabulate(NumWays)(_ => ThresholdCounter.Init))
// NumWays = NumBtbResultEntries = 4 × 2 = 8
// SRAM이 아닌 단순 레지스터 8개. PC/tag/set 차원 없음.
val thres = s2_thresholds(s2_wayIdx(i))  // fetch packet 내 branch slot 위치로만 인덱싱
```

- **PC-specific하지 않음**: 서로 다른 PC의 branch들이 같은 way-slot에 들어오면 동일한 threshold를 공유
- **Global하지도 않음**: 8개의 slot별로 독립적으로 학습 (slot 0은 slot 0의 branch들만의 SC 정확도를 반영)

**설계 배경 (Seznec TAGE-SC-L)**

Seznec의 원본 SC 논문에서 threshold는 **단 1개의 global value**였다. XiangShan은 이를 per-way-slot 8개로 세분화한 것으로, 원본보다 fine-grained하지만 여전히 per-PC는 아니다.

핵심 논거: threshold는 "SC가 TAGE를 override하려면 얼마나 강한 evidence가 필요한가"라는 **meta-level** 통계다. PC-specificity는 SC table entry(PathTable, BiasTable)가 이미 담당하므로, threshold 자체는 global/coarse-grained으로도 충분하다는 것이 Seznec의 주장.

**Pollution 문제 (미확인 trade-off)**

> 유저 지적: 서로 다른 PC가 같은 way-slot을 공유하면, 각 PC의 threshold 요구사항이 섞여 pollute될 수 있다.

- 이론적으로는 real trade-off: hard-to-predict branch A가 threshold를 높이면, 같은 slot의 easy-to-correct branch B도 높은 threshold를 받음
- Seznec의 반론: hard-to-predict branch들은 threshold 요구사항이 통계적으로 유사한 경향이 있음
- 실제 영향은 workload-dependent하며, 코드 레벨에서는 확인 불가 — **논문/시뮬레이션 수준에서 추가 확인 필요**

### Critical example: TAGE weakTaken + biasTable correction

The scenario below is where SC fires most decisively.

```
PC_B: blt  x1, x2, HANDLE   // inside validate()
```

**PHR aliasing 발생 구조**

```c
// Hot loop A (10,000 iters): data_a[] 값이 limit보다 거의 항상 크다 → NotTaken 90%
for (int i = 0; i < 10000; i++) {
    validate(data_a[i], limit);   // call site A, PC = 0x8000_0100
}

// Hot loop B (4,000 iters): data_b[] 값이 limit보다 거의 항상 작다 → Taken 85%
for (int i = 0; i < 4000; i++) {
    validate(data_b[i], limit);   // call site B, PC = 0x8000_0500
}

bool validate(int val, int limit) {
    if (val < limit) {   // Branch at PC_B = 0x8000_1000
        handle(val);
    }
}
```

TAGE index = `(PC_B_high) ^ fold(PHR, histLen)`. 두 call site는 PC가 다르지만 PHR folding 결과가 같을 수 있다:

```
Loop A → PHR when at PC_B: [0x8000_0100, prev_targets...]
  fold(PHR_A, hist=8) = 0x3A

Loop B → PHR when at PC_B: [0x8000_0500, prev_targets...]
  fold(PHR_B, hist=8) = 0x3A   ← PHR aliasing: 다른 경로인데 fold 값이 같음

→ 두 context가 TAGE의 같은 entry (SLOT_K)를 공유
```

SLOT_K의 학습 결과:
- Loop A: 10000 × 90% NotTaken = 9000 Not, 1000 Taken
- Loop B: 4000 × 85% Taken = 600 Not, 3400 Taken
- 합산: **9600 NotTaken, 4400 Taken**

Counter는 대체로 NotTaken 쪽이지만, Loop B가 연속으로 실행되면 counter가 +1(weakTaken)으로 밀려나는 순간들이 발생한다.

**TAGE = weakTaken (+1)인 순간**:

Loop B의 연속 Taken으로 counter가 +1로 올라간 직후, 다음 predict 대상이 Loop A일 경우:

```
provider SLOT_K: ctr = +1 (weakTaken)
tagePred = Taken   ← 실제는 NotTaken (Loop A 확률 90%)
tageConfHigh = false, tageConfLow = true
```

**BiasTable이 학습한 것**:

BiasTable은 history 없이 PC_B만으로 인덱싱하므로, "PC_B에서 TAGE가 weakTaken을 예측했을 때 실제 결과"를 누적한다. SLOT_K = +1 상태는 Loop B 직후에 빈번하므로, 이 시점의 실제 결과는 Loop A의 NotTaken이 많다:

```
BiasTable[PC_B, isWeak=1, taken=1] → ctr ≈ -5 (NotTaken bias 학습 완료)
```

**Step 1 — TAGE prediction (s2)**

```
provider table hit: ctr = +1 (weakTaken)
tagePred = Taken
tageConfHigh = false, tageConfLow = true   // weak counter
```

**Step 2 — SC sum accumulation (s2)**

`biasTable` index = `Cat(wayIdx, isWeak=1, taken=1)`
— the table has learned that "when TAGE predicts weakly-taken for PC_A, the actual outcome is not-taken ~70% of the time":

```
biasTable  ctr = -6  (strong not-taken bias)
pathTable  ctr = -2  (call path also votes not-taken)
sum = -8
```

**Step 3 — threshold comparison**

```scala
// Source: sc/Sc.scala:323-329
when(hit && valid && tageConfLow) {
  conf := aboveThreshold(sum, thres >> 3)  // threshold ÷ 8 → easy to intervene
}
// if thres = 8, effective threshold = 1
// |sum| = 8 ≥ 1 → conf = true
```

**Step 4 — override**

```scala
// Source: sc/Sc.scala:338-342
val finalPred = Mux(conf, !tagePred, tagePred)
// conf=true, tagePred=Taken → finalPred = NotTaken  ← SC flips the prediction
```

**Result**

| | TAGE only | TAGE + SC |
|---|---|---|
| prediction | Taken (wrong) | **NotTaken (correct)** |
| root cause | weakTaken accumulated via GHR aliasing | biasTable learned "(weakTaken → actually NotTaken)" pattern |

**Key point**: SC intervention is gated on `tageConfLow`. When the TAGE provider is saturated, the effective threshold is `thres >> 1` — much harder to cross — so SC stays silent. SC's role is precision correction specifically in the low-confidence region of TAGE.

---

## 1.1 Prediction Unit types and roles

SC (Statistical Corrector) is a corrector that corrects **TAGE prediction**.
When TAGE predicts taken/not-taken for a conditional branch, SC uses the sum (percsum) of multiple signed counter tables to decide whether to overturn the TAGE prediction.

- **Not a stand-alone predictor**: Receives TAGE’s provider prediction + mbtb hit results as input and corrects them.
- **Processing unit**: Processes NumBtbResultEntries (= NumWay × NumAlignBanks = 4 × 2 = **8**) entries of mbtb per-way.
- **Predicted distance**: Direction correction for the conditional branch of the current input block (no lookahead)

| Unit | Type | CFI per Entry | Predict Distance | Description |
|------|------|---------------|-----------------|------|
| SC | Statistical Corrector | 1 (per way) | current block | TAGE taken/not-taken correction |

---

## 1.2 Prediction Unit Memory Spec

### Parameter reference value (`Parameters.scala`)

```scala
// Source: sc/Parameters.scala:22
case class ScParameters(
    PathTableInfos:   Seq[ScTableInfo] = Seq(ScTableInfo(128, 8), ScTableInfo(128, 16)),
    GlobalTableInfos: Seq[ScTableInfo] = Seq(ScTableInfo(128, 8), ScTableInfo(128, 16)),
    BackwardTableInfos: Seq[ScTableInfo] = Seq(ScTableInfo(128, 4), ScTableInfo(128, 8)),
    BiasTableSize:       Int = 128,
    BiasUseTageBitWidth: Int = 2,
    PathEnable:   Boolean = true,
    GlobalEnable: Boolean = false,  // disabled by default
    BWEnable:     Boolean = false,  // disabled by default
    BiasEnable:   Boolean = true,
    CtrWidth:     Int = 6,
    ThresholdWidth: Int = 12,
    ThresholdInit:  Int = 720,
    NumBanks:       Int = 2,
    WriteBufferSize: Int = 4
)
```

### Calculated value

- `NumWays` = `NumBtbResultEntries` = `NumWay × NumAlignBanks` = 4 × 2 = **8**
- `BiasTableNumWays` = `NumWays << BiasUseTageBitWidth` = 8 << 2 = **32**
- `ScEntry` width = `CtrWidth` = **6 bits** (signed saturating counter)

### Memory specification table

| Table | Size (sets) | Width (bits) | #Tables | Banks | Read Ports | Write Ports | Enable | Detection Example |
|-------|------------|--------------|---------|-------|------------|-------------|--------|-------------------|
| PathTable[0] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank (via WriteBuffer) | **true** | `A()→[branch]` vs `C()→[branch]`: branch inside the same function is consistently NotTaken when called from A, Taken when called from C — 1-hop call-site bias, captured within hist=8 |
| PathTable[1] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | **true** | `A()→B()→[branch]` vs `C()→B()→[branch]`: branch inside B() changes direction depending on who called B() — 2-hop chain, requires hist=16 to distinguish the deeper caller |
| GlobalTable[0] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false | `if(x) { … if(y) … }`: inner branch strongly correlated with outer branch result ≤8 steps back in GHR (short inter-branch correlation) |
| GlobalTable[1] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false | Early guard check → late branch: a branch at the bottom of a function is correlated with a guard branch 9–16 branches earlier in GHR |
| BWTable[0] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false | 4-iteration inner loop: exit branch taken only on 4th iteration — short backward history (≤4) is sufficient to reveal the loop-exit pattern |
| BWTable[1] | 128 | 6×8=48 per row | 1 | 2 | 1 per bank | 1 per bank | false | 8-iteration loop or 2×4 nested loop: loop-exit branch requires up to 8 backward steps to distinguish the last iteration from earlier ones |
| BiasTable | 128 | 6×32=192 per row | 1 | 2 | 1 per bank | 1 per bank | **true** | TAGE accumulates `weakTaken(+1)` for a branch that is actually NotTaken ~70% of the time due to path history (PHR) aliasing. BiasTable slot `(PC, isWeak=1, taken=1)` learns the systematic flip and overrides TAGE |

> `singlePort = true`: Each bank has one physical read/write port. read first.

### Why separate Path/Global/BW/Bias into “different tables”

The key point is that **even if the SRAM wrapper (`ScTable`) of the same size is reused, the index input and statistics to learn are different**.

- PathTable: indexed by `PC ^ foldedPathHist`. Path history-based correlation learning.
- GlobalTable: Indexed with `PC ^ foldedGHR`. Global history-based correlation learning.
- BWTable: Indexed with `PC ^ foldedBW`. backward (loop) history-based correlation learning.
- BiasTable: Expands the way to `PC`-based set + `wayIdx + (providerWeak/providerTaken)` without history, directly learning **TAGE provider direction bias**.

```scala
// Source: sc/Helpers.scala:37-59
getPathTableIdx   = PC ^ foldedPathHist
getGlobalTableIdx = PC ^ foldedGhr
getBWTableIdx     = PC ^ foldedBW
getBiasTableIdx   = PC only

// Source: sc/Sc.scala:287-293
biasWayIdx = Cat(wayIdx, providerIsWeak, providerTaken)
```

```scala
// Source: sc/Parameters.scala:23-40, 74
PathTableInfos      = [(128, hist=8), (128, hist=16)]
GlobalTableInfos    = [(128, hist=8), (128, hist=16)]
BackwardTableInfos  = [(128, hist=4), (128, hist=8)]
BiasTableNumWays = NumWays << 2 // providerWeak/providerTaken reflects 2 bits
```

In other words, “SRAMs have similar shapes,” but **the learning signal space (feature space) is different, so separate tables** are correct.
In the current default setting, only `Path/Bias` is enabled and `Global/BW` is disabled as a function gate (`GlobalEnable/BWEnable`).

### Why are there two tables of the same type?

The only two fields in `ScTableInfo` are `Size` and `HistoryLength`:

```scala
// Source: bpu/Types.scala:132
class ScTableInfo(val Size: Int, val HistoryLength: Int)
```

Two tables of the same type have the same Size and only different HistoryLength:

| group | [0] Size / HistLen | [1] Size / HistLen |
|---|---|---|
| PathTable | 128 / **8** | 128 / **16** |
| GlobalTable | 128 / **8** | 128 / **16** |
| BWTable | 128 / **4** | 128 / **8** |

Different HistoryLengths result in different index calculations:

```scala
// Source: sc/Sc.scala:153-160
private val s0_pathIdx = PathTableInfos.map(info =>
  getPathTableIdx(
    s0_startPc,
    new FoldedHistoryInfo(info.HistoryLength, min(info.HistoryLength, log2Ceil(info.Size))),
    io.foldedPathHist,
    info.Size
  )
)
// PathTable[0]: PC_high ^ fold(pathHist[7:0],  7bit) → set index
// PathTable[1]: PC_high ^ fold(pathHist[15:0], 7bit) → another set index
```

**Both tables are read simultaneously (in parallel) from s0, and the results are summed**:

```scala
// Source: sc/Sc.scala:235-236
private val s1_pathPercsum =
  VecInit.tabulate(NumWays)(w =>
    s1_pathResp.map(entry => getPercsum(entry(w).ctr.value)).reduce(_ +& _)
  )
// [0].ctr.percsum + [1].ctr.percsum → additive vote, not winner-take-all
```

In other words, the two tables observe the same branch simultaneously with different history lengths and add up the results. This is the same philosophy as TAGE's having multiple history length tables in geometric sequence, where short history (fast learning, narrow context) and long history (slow learning, wide context) complement each other.

The reason why BWTable's history is shorter (4, 8 vs 8, 16): The backward/loop pattern has a short cycle, so a long history is unnecessary.

---

## 1.3 Memory Entry Description

### ScEntry Definition

```scala
// Source: sc/Bundles.scala:44
class ScEntry(implicit p: Parameters) extends ScBundle {
  val ctr: SignedSaturateCounter = Counter()
  // Counter.width = CtrWidth = 6 bits
}

// Source: bpu/SignedSaturateCounter.scala:21
class SignedSaturateCounter(width: Int) extends Bundle {
  val value: SInt = SInt(width.W)
  // range: [-32, 31] for width=6
  // positive (>=0) → predict taken
  // negative (<0)  → predict not-taken
  // weak: value==0 or value==-1
  // mid: !saturate && !weak
  // sat: value==31 or value==-32
}
```

### ScEntry Field Table

| Field Name | Width (bits) | Description |
|-----------|-------------|-------------|
| `ctr` | 6 | Signed saturating counter. `value >= 0` → taken, `< 0` → not-taken. When calculating percsum, expand to `Cat(ctr, 1.U(1.W)).asSInt` = `ctr*2+1` |

### percsum conversion

```scala
// Source: sc/Helpers.scala:61
def getPercsum(ctr: SInt): SInt = Cat(ctr, 1.U(1.W)).asSInt
// = ctr * 2 + 1
// CtrWidth=6 → ctr 범위 [-32, 31] → percsum 범위 [-63, 63]
```

| ctr value | percsum (ctr*2+1) | 상태 |
|---|---|---|
| +31 | +63 | saturate Taken |
| +1 ~ +30 | +3 ~ +61 | mid Taken |
| 0 | +1 | weak Taken (WeakPositive) |
| -1 | -1 | weak NotTaken (WeakNegative) |
| -2 ~ -31 | -3 ~ -61 | mid NotTaken |
| -32 | -63 | saturate NotTaken |

ctr=0(percsum=+1)과 ctr=-1(percsum=-1) 사이에 percsum=0이 존재하지 않는다. 모든 entry가 반드시 ±1 이상의 투표를 하며 dead zone이 없다.

### BiasTable Indexing

```scala
// Source: sc/Sc.scala:287
val s2_biasIdxLowBits = Cat(valid && ctr.isWeak, valid && taken)
// [1]: providerValid && providerCtr.isWeak
// [0]: providerValid && providerTaken
// biasWayIdx = Cat(wayIdx[2:0], biasIdxLowBits[1:0])  → 5-bit index into 32 ways
```

### Comparison of entries between PathTable and BiasTable

The ScEntry type is the same, but the table structure is fundamentally different.

```
             PathTable                   BiasTable
             ─────────                   ─────────
Set number 128 128 ← Same
Entry type ScEntry(ctr: 6bit) ScEntry(ctr: 6bit) ← Same
Number of Ways NumWays = 8 BiasTableNumWays = 32 ← 4 times
Set index PC ^ foldedPathHistory PC only (no history)
Way index     cfiPosition[2:0]            Cat(cfiPosition[2:0],
                                              providerIsWeak,
                                              providerTaken)
```

**Reason for 4 times the number of Ways**: `BiasTableNumWays = NumWays << BiasUseTageBitWidth = 8 << 2 = 32`.
Since the lower 2 bits of the way address are filled with the TAGE provider status `(isWeak, taken)`,
Even the same cfiPosition on the same PC accesses **four separate counter slots** depending on the TAGE status.

```scala
// Source: sc/Parameters.scala:74
def BiasTableNumWays: Int = NumWays << BiasUseTageBitWidth  // 8 << 2 = 32

// Source: sc/Sc.scala:287-293
val s2_biasIdxLowBits = Cat(valid && ctr.isWeak, valid && taken)
// bit[1]: providerValid && providerCtr.isWeak
// bit[0]: providerValid && providerTaken

val biasWayIdx = Cat(wayIdx, s2_biasIdxLowBits)
// = Cat( cfiPosition[2:0], providerIsWeak[1], providerTaken[0] )
// → Total 5-bit index → ​​Select 1 of 32 ways
```

What this structure means: Since there is a separate counter for each TAGE provider state,
“Actual results when TAGE predicts weak-taken” and “actual results when saturate-taken” are learned independently.

**Summary of Set index differences**:

```scala
// Source: sc/Helpers.scala:37-59
getPathTableIdx = (PC >> offset) ^foldedPathHist // mix history
getBiasTableIdx = (PC >> offset) // PC only (no history)
```

PathTable XORs the path history and refers to a different set if the call-path is different on the same PC.
BiasTable determines the set only through PC without history, and instead distinguishes TAGE status in the way dimension.

---

## 1.4 Paired BTB Description

SC does not work alone, but is combined with the following two units:

| Prediction Unit | Paired BTB/Predictor | Pairing Purpose | Combined Stage/Signal |
|----------------|---------------------|-------------|------------------|
| SC | mbtb (MainBTB) | Check conditional branch location (hitMask, wayIdx) | s2 / `sc.io.mbtbResult` |
| SC | TAGE | Provider Receives predicted values ​​and confidence (ctr), SC calibrates TAGE | s2 / `sc.io.providerTakenCtrs` |

```scala
// Source: bpu/Bpu.scala:230
sc.io.mbtbResult        := mbtb.io.result
sc.io.providerTakenCtrs := tage.io.toSc.providerTakenCtrVec

// Source: bpu/Bpu.scala:327
s2_condTakenMask = ... MuxCase(e.bits.taken,
  Seq(
useSc -> scTaken, // SC correction priority
    p.useProvider -> p.providerPred, // TAGE provider
    p.hasAlt      -> p.altPred       // TAGE alt
  ))
```

---

## 1.5 Predictive Pseudocode (with paired BTB)

```text
onPredict(startPc, foldedPathHist, commonHR, mbtbResult, providerTakenCtrs):

// --- Stage s0: Table index calculation + SRAM read request ---
  bankMask   = getBankMask(startPc)
  pathIdx[i] = PC[high] ^ foldedHist(PathTableInfos[i].HistLen)   (for i in 0..1)
  globalIdx[i] = PC[high] ^ fold(commonHR.ghr[HistLen-1:0])       (if commonHR.valid)
  bwIdx[i]   = PC[high] ^ fold(commonHR.bw[HistLen-1:0])          (if commonHR.valid)
  biasIdx    = PC[high]

  send read req to: pathTable[i], (globalTable[i] if GlobalEnable),
                    (bwTable[i] if BWEnable), biasTable

// --- Stage s1: Response collection + percsum accumulation (way unit) ---
  for each way w in 0..NumWays-1:
    pathPercsum[w]   = sum(getPercsum(pathResp[i][w].ctr)   for i)
    globalPercsum[w] = sum(getPercsum(globalResp[i][w].ctr) for i)  // 0 if !commonHR.valid
    bwPercsum[w]     = sum(getPercsum(bwResp[i][w].ctr)     for i)  // 0 if !commonHR.valid
    sumPercsum[w]    = pathPercsum[w] + globalPercsum[w] + bwPercsum[w]

// --- Stage s2: mbtb/TAGE combination + threshold judgment ---
  for each way w in mbtbResult:
    if not (mbtbResult[w].valid && isConditional): continue

    wayIdx        = getWayIdx(mbtbResult[w].cfiPosition)
    biasIdxLow    = Cat(providerCtr[w].isWeak && valid, providerTaken[w] && valid)
    biasWayIdx    = Cat(wayIdx, biasIdxLow)
    totalPercsum  = sumPercsum[wayIdx] + biasPercsum[biasWayIdx]

    scPred[w]     = totalPercsum >= 0

    threshold     = scThreshold[wayIdx].value >> 3  // base threshold
    if providerValid && tageConfHigh:
      conf = aboveThreshold(totalPercsum, threshold >> 1)
    elif providerValid && tageConfMid:
      conf = aboveThreshold(totalPercsum, threshold >> 2)
    elif providerValid && tageConfLow:
      conf = aboveThreshold(totalPercsum, threshold >> 3)
    else:
      conf = false

    useScPred[w]     = conf && providerValid && mbtbHit[w]
    sumAboveThres[w] = aboveThreshold(totalPercsum, threshold)

  output:
scTakenMask[w] = scPred[w] // Predict raw direction of SC
scUsed[w] = useScPred[w] // Whether to actually override TAGE
    meta = { scPathResp, scBiasResp, scPred, tagePred, useScPred, sumAboveThres, ... }

// aboveThreshold(sum, thres): |sum| > thres (check with sign)
```

---

## 1.6 Input-to-Output Latency and Throughput

| Unit | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|------|------------|-------------|----------------|------------------------|
| SC (scTakenMask, scUsed) | s0 (SRAM req) | s2 (combinatorial output) | 2 | 1 block / cycle |
| SC meta | s0 | s3 (RegEnable of s2_fire) | 3 | 1 block / cycle |

> SRAM: `holdRead=true` → Response from s1 is valid.
> The s2 output is combinatorial (combining the mbtb/TAGE s2 signal to the s1 result).

### Why are `SC(scTakenMask/scUsed)` and `SC meta` latency different?

- `scTakenMask`, `scUsed` are calculated directly in s2 and output immediately.

```scala
// Source: sc/Sc.scala:342-343
io.scTakenMask := s2_scPred
io.scUsed      := s2_useScPred
```

- `meta` registers **s2 results once more** for FTQ training and delivers them to s3.

```scala
// Source: sc/Sc.scala:349-360
io.meta.scPathResp      := ... RegEnable(..., s2_fire)
io.meta.scPred          := RegEnable(s2_scPred, s2_fire)
io.meta.useScPred       := RegEnable(s2_useScPred, s2_fire)
io.meta.sumAboveThres   := RegEnable(s2_sumAboveThres, s2_fire)
```

organize:
- The prediction decision signal (`scTakenMask/scUsed`) is used directly in the final MUX of BPU s2, so latency 2.
- Learning meta (`meta`) is transferred to s3 for stage matching/FTQ storage, so latency 3.

---

## 1.7 Pipeline Stage Location

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
|--------|-----------------|-----------------|-------------|
| `s0_pathIdx`, `s0_biasIdx` | s0 | s0 (SRAM req) | combinatorial from startPc + foldedPathHist |
| `s0_globalIdx`, `s0_bwIdx` | s0 | s0 (SRAM req) | gated by `commonHR.valid` |
| `s1_pathResp`, `s1_biasResp` | s1 | s1 (percsum calculation) | SRAM 1-cycle read latency |
| `s1_sumPercsum[w]` | s1 | s2 (RegEnable s1_fire) | path+global+bw sum |
| `mbtbResult` input | s2 | s2 | mbtb result same cycle |
| `providerTakenCtrs` input | s2 | s2 | TAGE s2 output |
| `scTakenMask`, `scUsed` | s2 | s2 (Bpu.scala combined) | s2 combinatorial output |
| `meta.*` | s2 → s3 | FTQ (training) | RegEnable(s2_fire) |
| `scThreshold[w]` | trained t1 | s2 | Reg, updated at t1 |

```scala
// Source: sc/Sc.scala:57-60
private val s0_fire = io.stageCtrl.s0_fire && io.enable
private val s1_fire = io.stageCtrl.s1_fire && io.enable
private val s2_fire = io.stageCtrl.s2_fire && io.enable
private val s3_fire = io.stageCtrl.s3_fire && io.enable

// Source: sc/Sc.scala:95
private val s1_commonHR = RegEnable(s0_commonHR, s0_fire)
private val s2_commonHR = RegEnable(s1_commonHR, s1_fire)

// Up to 3 cycles backfill with state machine when GHR override
// idle → state1 → state2 → state3 → idle
```

---

## 1.8 Training method

### Training Trigger

- FTQ resolve → `t0_fire` (= `io.stageCtrl.t0_fire`)
- t1: `t1_train = RegEnable(io.train, t0_fire)` → Process after 1 cycle delay

### SC Table entry 업데이트 조건

```scala
// Source: sc/Helpers.scala:84-85
val needUpdate = writeValid && writeWayIdx === wayIdx &&
  metaData.tagePredValid(branchIdx) &&
  (metaData.scPred(branchIdx) =/= writeTaken   // ① SC가 틀렸다
   || !metaData.sumAboveThres(branchIdx))       // ② sum이 threshold 미달
// → ctr을 actual taken 방향으로 ±1
```

조건을 만족하면 모든 SC table (PathTable, GlobalTable, BWTable, BiasTable)의 해당 entry ctr을 actual taken 방향으로 `getUpdate(writeTaken)` 적용.

| 케이스 | 조건 | 동작 | 이유 |
|---|---|---|---|
| ① SC 틀림 | `scPred ≠ actual` | ctr → actual 방향 | 잘못된 방향 수정 |
| ② sum 미달 | `!sumAboveThres` | ctr → actual 방향 | SC가 override 안 했어도 evidence 축적 |

**케이스 ②**: SC가 threshold를 못 넘어 override하지 못했더라도 (TAGE가 맞았든 틀렸든) 계속 학습한다. 다음 번에 더 강한 sum을 만들기 위해서. TAGE와 SC가 같은 예측을 했을 때도 조건 ①②가 맞으면 학습한다.

### Threshold 구조

```scala
// Source: sc/Sc.scala:78, sc/Parameters.scala:42-43
val scThreshold = RegInit(VecInit.tabulate(NumWays)(_ => ThresholdCounter.Init))
// ThresholdCounter: SaturateCounter (unsigned), width=12, init=720
// 범위: [0, 4095]  (unsigned)
```

예측 시 실제 사용 값:

```scala
// Source: sc/Sc.scala:309, 317
val s2_thresholds = scThreshold.map(_.value >> 3)   // base threshold = value >> 3
val thres = s2_thresholds(wayIdx)
```

| tageConf | 비교 threshold | 공식 | init(720) 기준 |
|---|---|---|---|
| tageConfHigh (saturate) | `thres >> 1` | `value >> 4` | 720 >> 4 = **45** |
| tageConfMid | `thres >> 2` | `value >> 5` | 720 >> 5 = **22** |
| tageConfLow (weak) | `thres >> 3` | `value >> 6` | 720 >> 6 = **11** |
| sumAboveThres (training용) | `thres` | `value >> 3` | 720 >> 3 = **90** |

```scala
// Source: sc/Helpers.scala:63-64
def aboveThreshold(scSum: SInt, threshold: UInt): Bool =
  (scSum > threshold.zext) && pos(scSum) ||
  (scSum < -threshold.zext) && neg(scSum)
// 즉 |sum| > threshold (부호 방향 일치 포함)
```

### Threshold 업데이트 조건

```scala
// Source: sc/Sc.scala:451-456
val scWrong    = taken =/= t1_meta.scPred(branchIdx)
val shouldUpdate = writeValid && writeWayIdx === wayIdx &&
  t1_meta.tagePredValid(branchIdx) &&
  (t1_meta.tagePred(branchIdx) =/= t1_meta.scPred(branchIdx)) &&  // SC와 TAGE가 달랐을 때만
  (scWrong || !t1_meta.sumAboveThres(branchIdx))
prevThres.getUpdate(scWrong, en = shouldUpdate)
// ThresholdCounter(unsigned): scWrong=true → +1 (증가), false → -1 (감소)
```

table 조건과의 차이: **`tagePred ≠ scPred`** 조건이 추가됨. TAGE와 SC가 같은 방향을 예측했을 때는 threshold를 건드리지 않는다.

| 케이스 | 조건 | threshold | 이유 |
|---|---|---|---|
| SC가 override 후 틀림 | `tagePred≠scPred` AND `scWrong=true` AND `sumAboveThres` | **증가** | SC가 틀린 override → 더 보수적으로 |
| SC가 맞지만 sum 미달로 override 못함 | `tagePred≠scPred` AND `scWrong=false` AND `!sumAboveThres` | **감소** | SC가 맞지만 bar가 너무 높았음 |
| SC가 override 후 맞음 | `tagePred≠scPred` AND `scWrong=false` AND `sumAboveThres` | **변화 없음** | shouldUpdate = false |

### 두 조건의 관계

```
branch retired (actual taken 확인)
        │
        ├─ tagePredValid?  No → 아무것도 안 함
        │
        Yes
        ├─ [SC table update]  tagePredValid AND (scPred≠actual OR !sumAboveThres)
        │    → 모든 table의 해당 entry ctr을 actual 방향으로 ±1
        │
        └─ [Threshold update]  tagePredValid AND tagePred≠scPred AND (scWrong OR !sumAboveThres)
             → threshold를 scWrong 방향으로 ±1
             (TAGE와 SC가 동일 예측이면 threshold 불변)
```

**요약**: table은 "방향이 맞는지"를 학습하고, threshold는 "SC가 override해도 되는 강도"를 학습한다.

### FTQ Archive Information

```scala
// Source: sc/Bundles.scala:69
class ScMeta(implicit p: Parameters) extends ScBundle with HasScParameters {
  val scPathResp:      Vec[Vec[UInt]] // NumPathTables × NumWays × 6 bits
  val scGlobalResp:    Vec[Vec[UInt]] // NumGlobalTables × NumWays × 6 bits
  val scBWResp:        Vec[Vec[UInt]] // NumBWTables × NumWays × 6 bits
  val scBiasResp:      Vec[UInt]      // BiasTableNumWays(=32) × 6 bits
val scBiasLowerBits: Vec[UInt] // NumWays × 2 bits (TAGE confidence lower bits)
val scCommonHR: CommonHREntry // ghr/bw history (for index recalculation during training)
val scPred: Vec[Bool] // NumWays: SC raw direction prediction
val tagePred: Vec[Bool] // NumBtbResultEntries: TAGE provider prediction
  val tagePredValid:   Vec[Bool]      // NumBtbResultEntries: TAGE provider valid
val useScPred: Vec[Bool] // NumWays: Whether SC actually overrides
val sumAboveThres: Vec[Bool] // NumWays: Is the sum above the threshold?
}
```

### Training flow summary

| Trigger | Required FTQ Info | FTQ Storage | Write Port / Conflict Handling |
|---------|-------------------|-------------|-------------------------------|
| FTQ resolve (t0_fire) | scPathResp, scBiasResp, scCommonHR, scPred, tagePred, tagePredValid, useScPred, sumAboveThres | `io.train.meta.sc` (ScMeta) | WriteBuffer(size=4) per bank. read first: `bank.io.w.req.valid := buffer.valid && !bank.io.r.req.valid` |

### WriteBuffer operation

```scala
// Source: sc/ScTable.scala:108-113
sram.zip(writeBuffer).foreach { case (bank, buffer) =>
  bank.io.w.req.valid  := buffer.io.read.head.valid && !bank.io.r.req.valid
// Write only when there is no read. Wait in WriteBuffer when read crashes
  buffer.io.read.head.ready := bank.io.w.req.ready && !bank.io.r.req.valid
}
```

- **write priority**: Write only when there is no read request → read-first
- **Conflict handling**: WriteBuffer(FIFO, depth=4) stores pending writes
- **wayMask-based selective write**: Selectively update only the changed way to `wayMask`

---

## Quality Checklist

- [x] Comply with order 1.1~1.8
- [x] specify memory depth/width/tables/banks/read/write ports
- [x] Includes memory entry code snippet
- [x] Completion of width/description table for each field
- [x] specify pair BTB combining rules
- Write pseudocode including [x] paired BTB
- [x] latency/throughput quantification
- [x] stage input/output timing specified
- [x] Specify training trigger/FTQ storage/meta fields/port conflict processing

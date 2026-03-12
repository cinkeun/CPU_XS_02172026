# TAGE Prediction Unit Analysis

> Analysis principle: All content is **code-based only**. tage/ directory (Parameters.scala, Abstracts.scala, Bundles.scala, Helpers.scala, TageTable.scala, Tage.scala) and bpu/Bundles.scala, bpu/Bpu.scala code base.

---

## 1.1 Prediction Unit types and roles

TAGE is a conditional branch direction predictor of the **TAgged GEometric history length predictor** series.
It predicts the direction of one CFI (conditional branch) per entry, and generates predictions independently for each branch position within the block (`NumBtbResultEntries`).
The prediction target is prediction of the **"next block"** of the current input block (complementary direction to the branch candidate provided by mBTB).

- 8 tables: exponential increase from history length 4 to 397
- Prediction logic: hit table with longest history = provider, then = alt
- `useAltOnNa`: If provider counter is weak (weak saturation) and `useAltOnNaVec` is positive, use alt
- Linked with SC (Statistical Corrector): Provider counter delivered to `tage.io.toSc.providerTakenCtrVec`

```scala
// Source: bpu/tage/Tage.scala:149-155
val useProvider = hasProvider && (!useAltOnNa || !provider.takenCtr.isWeak)
io.prediction(i).useProvider  := useProvider
io.prediction(i).providerPred := provider.takenCtr.isPositive
io.prediction(i).hasAlt       := hasAlt
io.prediction(i).altPred      := alt.takenCtr.isPositive
```

| Unit | Type | CFI per Entry | Predict Distance | Description |
|------|------|---------------|-----------------|------|
| TAGE | TAGE (8 tables, geometric history) | 1 conditional branch per position | Next block (per mBTB candidate) | provider/alt selection, provides SC post-processing input |

---

## 1.2 Prediction Unit Memory Spec

Parameter (`Parameters.scala:23-45`):

```scala
// Source: bpu/tage/Parameters.scala:24-44
TableInfos: Seq[TageTableInfo] = Seq(
  new TageTableInfo(4096, 2, 4),    // Table-0: histLen=4
  new TageTableInfo(4096, 2, 9),    // Table-1: histLen=9
  new TageTableInfo(4096, 2, 17),   // Table-2: histLen=17
  new TageTableInfo(4096, 2, 29),   // Table-3: histLen=29
  new TageTableInfo(4096, 2, 56),   // Table-4: histLen=56
  new TageTableInfo(4096, 2, 109),  // Table-5: histLen=109
  new TageTableInfo(4096, 2, 211),  // Table-6: histLen=211
  new TageTableInfo(4096, 2, 397)   // Table-7: histLen=397
),
NumBanks:       Int = 4,
TagWidth:       Int = 13,
TakenCtrWidth:  Int = 3,
UsefulCtrWidth: Int = 2,
WriteBufferSize: Int = 4,
```

`TageTableInfo(Size, NumWays, HistoryLength)`: Size=4096 = NumSets × NumBanks × NumWays  
→ NumSets = 4096 / (4 banks × 2 ways) = **512 sets per bank**

Each table is:
- **entrySram**: `SRAMTemplate`, single-port, `NumBanks × NumWays = 4 × 2 = 8` SRAM instance
- **usefulCtrs**: `RegInit` 3D array (FF-based, not SRAM)

```scala
// Source: bpu/tage/TageTable.scala:49-73
private val entrySram =
  Seq.tabulate(NumBanks, NumWays) { (bankIdx, wayIdx) =>
    Module(new SRAMTemplate(
      new TageEntry, set = NumSets, way = 1,
      singlePort = true, ...
    ))
  }
private val usefulCtrs = RegInit(
  VecInit.fill(NumBanks)(VecInit.fill(NumWays)(VecInit.fill(NumSets)(UsefulCounter.Zero)))
)
```

**Write Buffer**: 1 per bank, size=4, `numPorts=NumWays=2`
→ To alleviate SRAM single-port conflict, drain the write request when there is no read from the write buffer.

| Memory/Table | Depth | Width (bit) | #Tables | Banks | Read Ports | Write Ports (via WB) |
|---|---|---|---|---|---|---|
| entrySram (each bank, way) | 512 (sets) | 1+13+3 = 17 | 8 | 4 | 1 (shared predict/train, mutex) | 1 (single-port SRAM, via WriteBuffer) |
| usefulCtrs (FF) | 512 (sets) | 2 | 8 × 4 banks × 2 ways | - | 1 | 1 |
| useAltOnNaVec | NumUseAltOnNa=128 | 7 | - | - | 1 | 1 |
| usefulResetCtr | 1 | 8 | - | - | - | 1 |

> Since predict read / train read share **same SRAM single port**, `assert(!(predictReadValid && trainReadValid))` — in case of bank conflict, stall to `io.trainReady := false`

---

## 1.3 Prediction Unit Memory Entry Description

### TageEntry (SRAM per way)

```scala
// Source: bpu/tage/Bundles.scala:54-58
class TageEntry(implicit p: Parameters) extends TageBundle {
  val valid:    Bool            = Bool()              // 1 bit
  val tag:      UInt            = UInt(TagWidth.W)   // 13 bit
  val takenCtr: SaturateCounter = TakenCounter()     // 3 bit (TakenCtrWidth)
}
```

| Field Name | Width (bit) | Description |
|---|---|---|
| `valid` | 1 | Entry validity |
| `tag` | 13 (`TagWidth`) | PC + position XOR folded history hash based tag |
| `takenCtr` | 3 (`TakenCtrWidth`) | Saturating taken counter |

### Separate usefulCtrs (FF)

| Field Name | Width (bit) | Description |
|---|---|---|
| `usefulCtr` | 2 (`UsefulCtrWidth`) | Usability counter, when provider is better than alt increment |

### TageMetaEntry (meta stored in FTQ)

```scala
// Source: bpu/tage/Bundles.scala:108-115
class TageMetaEntry(implicit p: Parameters) extends TageBundle {
  val useProvider:       Bool            = Bool()
  val providerTableIdx:  UInt            = UInt(TableIdxWidth.W)    // log2(8) = 3 bit
  val providerWayIdx:    UInt            = UInt(MaxNumWays.W)       // 2 bit
  val providerTakenCtr:  SaturateCounter = TakenCounter()           // 3 bit
  val providerUsefulCtr: SaturateCounter = UsefulCounter()          // 2 bit
  val altOrBasePred:     Bool            = Bool()                   // 1 bit
}
```

| Field Name | Width (bit) | Description |
|---|---|---|
| `useProvider` | 1 | Whether to use provider prediction |
| `providerTableIdx` | 3 | provider table index |
| `providerWayIdx` | 2 | provider way index |
| `providerTakenCtr` | 3 | provider counter value at the time of prediction |
| `providerUsefulCtr` | 2 | provider useful counter value at the time of prediction |
| `altOrBasePred` | 1 | alt pred or base(mBTB) pred |

---

## 1.4 Pair BTB Unit Description

TAGE operates paired with **mBTB (MainBTB)**.

```scala
// Source: bpu/tage/Tage.scala:38-41
val fromMainBtb: MainBtbToTageIO = new MainBtbToTageIO
// Source: bpu/Bpu.scala:222
tage.io.fromMainBtb.result := mbtb.io.result
```

- mBTB provides branch candidates (`NumBtbResultEntries`) in s2
- TAGE generates per-branch predictions by XORing `cfiPosition` of each branch candidate to tag.
- In prediction meta, `altOrBasePred` falls back to `branch.bits.taken` when no alt provider exists.
- When training, TAGE looks up `meta.mbtb.entries` of mBTB and refers to the counter (base pred) of the branch.

```scala
// Source: bpu/tage/Tage.scala:117-125
s2_branches.zipWithIndex.foreach { case (branch, i) =>
  val position = branch.bits.cfiPosition
  val tag      = s2_rawTag(tableIdx) ^ position  // position is XOR'd into tag
  ...
  io.meta.entries(i).altOrBasePred := Mux(hasAlt, alt.takenCtr.isPositive, branch.bits.taken)
}
```

| Prediction Unit | Paired BTB | Pairing Purpose | Combined Stage/Signal |
|---|---|---|---|
| TAGE | mBTB (MainBTB) | Receive branch candidates, supplement direction, provide base pred | s2: `io.fromMainBtb.result` → per-branch tag XOR position, altOrBasePred fallback |

---

## 1.5 Next Prediction Pseudocode (with paired BTB)

```text
onPredict(startPc, foldedPathHist, mBTBresult[]):

// s0: Send SRAM read request
  for each table t:
    bankIdx = getBankIndex(startPc)
    setIdx  = getSetIndex(startPc, t.foldedHist.forIdx)
    t.entrySram[bankIdx].sendReadReq(setIdx)
  
// s1: Receive SRAM read resp (1 cycle latency), calculate rawTag
  for each table t:
    rawTag[t] = getTag(startPc) XOR t.foldedHist.forTag

// s2: mBTB result reception, per-branch prediction
  for each branch b in mBTBresult:
    position = b.cfiPosition
    useAltOnNa = useAltOnNaVec[getUseAltOnNaIdx(cfiPc(startPc, position))].isPositive

    for each table t:
      tag = rawTag[t] XOR position    // position hashed into tag
      hit[t] = (readEntry.valid && readEntry.tag == tag)
      takenCtr[t] = readEntry.takenCtr (from 1-cycle delayed SRAM resp)

    // provider = longest history hit table
    providerTableOH = PriorityEncoderOH(hitTableMask.reverse).reverse
    provider = Mux1H(providerTableOH, allTableResults)
    alt      = second longest history hit table

    useProvider = hasProvider && (!useAltOnNa || !provider.takenCtr.isWeak)
    providerPred = provider.takenCtr.isPositive
    altPred      = alt.takenCtr.isPositive

    output prediction[b]:
      useProvider  = useProvider
      providerPred = providerPred
      hasAlt       = hasAlt
      altPred      = altPred

    output toSc[b]:
providerTakenCtr = provider.takenCtr // SC refers to TAGE counter

    output meta[b]:
      providerTableIdx, providerWayIdx, providerTakenCtr, providerUsefulCtr, altOrBasePred
```

---

## 1.6 Input-to-Output Latency and Throughput

Prediction Pipeline:
- **s0**: receive startPc → send SRAM read req (`s0_fire && io.enable`)
- **s1**: SRAM read resp reception (1 cycle), rawTag calculation (`RegEnable(s0_*)`)
- **s2**: Receive mBTB result, tag match, select provider, output prediction (`RegEnable(s1_*)`)

```scala
// Source: bpu/tage/Tage.scala:93-95, 111-113
private val s1_startPc    = RegEnable(s0_startPc, s0_fire)
private val s1_foldedHist = RegEnable(s0_foldedHist, s0_fire)
private val s2_startPc  = RegEnable(s1_startPc, s1_fire)
private val s2_rawTag   = RegEnable(s1_rawTag, s1_fire)
private val s2_readResp = RegEnable(s1_readResp, s1_fire)
```

| Unit | Input Stage | Output Stage | Latency (cycle) | Throughput (pred/cycle) |
|--|--|--|--|--|
| TAGE | s0 | s2 | 2 | 1 (NumBtbResultEntries branches per cycle) |

---

## 1.7 Pipeline Stage Location (Input/Output Timing)

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
|--|--|--|--|
| `io.startPc`, `foldedPathHist` | s0 | s0 | SRAM read req sent |
| SRAM read resp | s1 | s1 | `DataHoldBypass(tables.map(_.io.predictReadResp), RegNext(s0_fire))` |
| `s1_rawTag` | s1 | s2 (`RegEnable(s1_fire)`) | After calculating tag, register it in s2 |
| `io.fromMainBtb.result` | s2 | s2 | mBTB s2 results arrive at TAGE s2 simultaneously, position → tag XOR |
| `io.prediction[]` | s2 | s2 (BPU top s2) | Effective immediately in s2 |
| `io.meta` | s2 | BPU top s3 (`RegEnable(tage.io.meta, s2_fire)`) | Save to FTQ from s3 |
| `io.toSc.providerTakenCtrVec` | s2 | SC(s2) | SC receives TAGE counter from s2 |

train route:

| Signal | Produced @ Stage | Consumed @ Stage | Timing Note |
|--|--|--|--|
| `io.train` (from FTQ resolve) | t0 | t0 | resolve time branch info + BpuResolveMeta |
| SRAM train read req | t0 | t0 | `t0_fire && !t0_useMeta && !bankConflict` |
| train SRAM read resp | t1 | t1 | `RegEnable(t0_fire)` |
| t2 update/alloc write | t2 | t2 (→ WriteBuffer) | `RegEnable(t1_fire)`, SRAM write via WriteBuffer |

---

## 1.8 Training method

### Training Trigger
- **t0_fire** = `io.stageCtrl.t0_fire && t0_hasCond && io.enable`
- Trigger: After resolution is completed from FTQ (`BpuTrain`) — actual taken/mispredict results are received
- **No Fast-train**: TAGE only performs resolve (commit) based train

```scala
// Source: bpu/tage/Tage.scala:189
private val t0_fire = io.stageCtrl.t0_fire && t0_hasCond && io.enable
```

### Meta reuse (`useMeta`) optimization

```scala
// Source: bpu/tage/Tage.scala:202-210
private val t0_useMeta = t0_branches.zipWithIndex.map { case (branch, i) =>
  val mbtbHit      = t0_mbtbHitMask(i)
  val isCond       = t0_condMask(i)
  val useProvider  = t0_meta(i).useProvider
  val mispredicted = branch.bits.mispredict
  !(mbtbHit && isCond) || (useProvider && !mispredicted)
}.reduce(_ && _)
private val t0_needRead = !t0_useMeta
```

- If all conditional branches that hit mBTB use the provider and there is no misprediction → **Read directly from meta and skip SRAM read** (`t0_useMeta = true`)
- If even one mispredict or non-provider path → Perform SRAM re-read

### Information that FTQ must retain

```scala
// Source: bpu/Bundles.scala:278-286, bpu/Bpu.scala:401
class BpuResolveMeta(implicit p: Parameters) extends BpuBundle {
  val mbtb:   MainBtbMeta = new MainBtbMeta
  val tage:   TageMeta    = new TageMeta    // ← TAGE meta
  val sc:     ScMeta      = new ScMeta
  val ittage: IttageMeta  = new IttageMeta
  ...
}
// Source: bpu/Bpu.scala:401
s3_resolveMeta.tage := RegEnable(tage.io.meta, s2_fire)
```

Meta structure:

```scala
// Source: bpu/tage/Bundles.scala:117-119
class TageMeta(implicit p: Parameters) extends TageBundle {
  val entries: Vec[TageMetaEntry] = Vec(NumBtbResultEntries, new TageMetaEntry)
}
```

| Field | Width | Description |
|--|--|--|
| `entries[i].useProvider` | 1 bit | Whether to use provider in ith branch |
| `entries[i].providerTableIdx` | 3bit | provider table index |
| `entries[i].providerWayIdx` | 2bit | provider way index |
| `entries[i].providerTakenCtr` | 3bit | Prediction point provider counter |
| `entries[i].providerUsefulCtr` | 2bit | Prediction time useful counter |
| `entries[i].altOrBasePred` | 1 bit | alt or base prediction |

### t2 Update and Allocate policy

- **needUpdateProvider**: If hit and not `notNeedUpdate` → updated takenCtr
- **needUpdateAlt**: if useAlt and not `notNeedUpdate` → update alt takenCtr
- **useAltOnNa update**: If the provider is a weak counter and alt is correct, increment, if incorrect, decrement
- **Allocate**: mispredict && (finalPred != actualTaken) && when provider is not the top table.
- New entry: Select `!entry.valid || (takenCtr.isWeak && usefulCtr.isSaturateNegative)` from the history table longer than the provider
- When selection fails, `usefulResetCtr` increases → When saturated, all usefulCtrs are reset.

### Write Port/Conflict handling

```scala
// Source: bpu/tage/TageTable.scala:77-86
private val entryWriteBuffers =
  Seq.tabulate(NumBanks) { bankIdx =>
    Module(new WriteBuffer(
      new EntrySramWriteReq,
      WriteBufferSize,  // = 4
      numPorts = NumWays,  // = 2
      ...
    ))
  }
// Source: bpu/tage/TageTable.scala:117
val valid = readPort.valid && !way.io.r.req.ready  // write only when SRAM read not used
```

| Trigger | Required FTQ Info | FTQ Storage | Write Port / Conflict Handling |
|--|--|--|--|
| `t0_fire` (resolve) | `TageMeta` (per branch: providerTableIdx, WayIdx, TakenCtr, UsefulCtr, altOrBasePred) | `BpuResolveMeta.tage` (s3 RegEnable) | WriteBuffer(size=4, 2 write ports per bank); SRAM single-port: write blocked when read active; In case of bank conflict, `trainReady=false` stall |

---

## 1.8 TAGE Table Indexing and Hashing Method

### AddrField layout

```scala
// Source: bpu/tage/Helpers.scala:51-58
val addrFields = AddrField(
  Seq(
    ("instOffset", instOffsetBits),  // PC[0:0]   (1 bit, RVC 2B-align offset)
    ("bankIdx",    BankIdxWidth),    // PC[2:1]   (2 bit, log2Ceil(NumBanks=4))
    ("setIdx",     SetIdxWidth),     // PC[11:3]  (9 bit, log2Ceil(NumSets=512))
    ("tag",        TagWidth)         // PC[24:12] (13 bit)
  )
)
```

- All 8 tables share the same `Size=4096`, `NumWays=2`, `NumBanks=4`
- `NumSets = Size / NumWays / NumBanks = 4096 / 2 / 4 = 512`, `SetIdxWidth = 9`
- PC bit layout is identical across all tables; **only the folded history width varies per table**

### Predict path index calculation

```scala
// Source: bpu/tage/Helpers.scala:61-68
def getBankIndex(pc: PrunedAddr): UInt =
  addrFields.extract("bankIdx", pc)                    // PC[2:1], no history

def getSetIndex(pc: PrunedAddr, hist: UInt): UInt =
  addrFields.extract("setIdx", pc) ^ hist              // PC[11:3] XOR foldedHist.forIdx

def getRawTag(pc: PrunedAddr, hist: UInt): UInt =
  addrFields.extract("tag", pc) ^ hist                 // PC[24:12] XOR foldedHist.forTag
```

**Per-branch final tag** (XOR with cfiPosition at s2):
```scala
// Source: bpu/tage/Tage.scala:125
val tag = s2_rawTag(tableIdx) ^ position   // rawTag XOR cfiPosition
```

**useAltOnNa index**:
```scala
// Source: bpu/tage/Helpers.scala:41-44
def getUseAltOnNaIdx(pc: PrunedAddr): UInt =
  pc(log2Ceil(NumUseAltOnNa) - 1 + instOffsetBits, instOffsetBits)
  // = cfiPC[7:1] (7 bits, NumUseAltOnNa=128)
```

### Folded history calculation (per table)

```scala
// Source: bpu/tage/Helpers.scala:27-36
def getFoldedHist(...): Vec[TageFoldedHist] = VecInit(TableInfos.map { implicit tableInfo =>
  val tageFoldedHist = tableInfo.getTageFoldedHistoryInfo(NumBanks, TagWidth).map { histInfo =>
    allFoldedPathHist.getHistWithInfo(histInfo).foldedHist
  }
  foldedHist.forIdx := tageFoldedHist.head                               // fold[0]
  foldedHist.forTag := tageFoldedHist(1) ^ Cat(tageFoldedHist(2), 0.U(1.W))  // fold[1] XOR (fold[2] << 1)
})

// Source: bpu/Types.scala:67-78 (getTageFoldedHistoryInfo)
// fold[0]: FoldedHistoryInfo(histLen, min(histLen, SetIdxWidth))   → used for setIdx
// fold[1]: FoldedHistoryInfo(histLen, min(histLen, TagWidth))      → used for tag (high)
// fold[2]: FoldedHistoryInfo(histLen, min(histLen, TagWidth-1))    → used for tag (low, shifted)
```

`forTag = fold(histLen, min(histLen, 13)) XOR Cat(fold(histLen, min(histLen, 12)), 0)`
→ Two independent folds of different widths are combined to produce a 13-bit tag component.

### Per-table fold width summary

All tables: `NumSets=512`, `SetIdxWidth=9`, `TagWidth=13`

| Table | histLen | forIdx fold width | forTag: fold[1] width | forTag: fold[2] width |
|-------|---------|-------------------|-----------------------|-----------------------|
| 0 | 4 | min(4,9) = **4** | min(4,13) = **4** | min(4,12) = **4** |
| 1 | 9 | min(9,9) = **9** | min(9,13) = **9** | min(9,12) = **9** |
| 2 | 17 | min(17,9) = **9** | min(17,13) = **13** | min(17,12) = **12** |
| 3–7 | 29–397 | **9** (saturated) | **13** (saturated) | **12** (saturated) |

Tables 0–1: histLen is short enough that no saturation occurs — fold width = histLen.
Tables 2–7: fold widths are fully saturated at SetIdxWidth/TagWidth limits.

### History source used by TAGE indexing: PHR

TAGE indexing/tagging receives folded history from PHR:

```scala
// Source: bpu/Bpu.scala:223
tage.io.fromPhr.foldedPathHist := phr.io.s0_foldedPhr
```

PHR (Predicted History Register) stores **path hash** bits, not branch direction (taken/not-taken):

```scala
// Source: bpu/history/phr/Helpers.scala:60-63
def pathHash(pc: PrunedAddr, target: PrunedAddr): UInt =
  (Cat(pc(9, 1), 0.U(4.W)) ^ target(16, 2))(PathHashWidth-1, 0)  // PC[9:1] ^ target[16:2]

// Source: bpu/history/phr/Phr.scala:131-133
private val hash      = pathHash(updateCfiPc, updateTarget)
private val shiftBits = hash(Shamt - 1, 0)              // hash[1:0]  → pushed into phr buffer
private val hashHigh  = hash(PathHashWidth - 1, Shamt)  // hash[14:2] → XOR'd into phr buffer

// Source: bpu/history/phr/Phr.scala:141-149
when(updateData.taken) {   // ← only taken branches trigger update
  phr[(phrPtr - i)] := shiftBits           // hash[1:0] shifted in
  phr[(phrPtr + i)] := hashHigh ^ phrLowBits  // hash[14:2] XOR'd in
}
```

The phr buffer is folded directly without any additional hash mixing:

```scala
// Source: bpu/history/phr/Phr.scala:158-161
s0_foldedPhr.getHistWithInfo(info).foldedHist :=
  computeFoldedHist(phrValue, info.FoldedLength)(info.HistoryLength)
// phrValue = raw phr circular buffer = accumulated path hash bits
```

### commonHR vs PHR in indexing

For **TAGE** specifically, indexing/hash inputs are PC fields + **PHR folded history** only:
- `setIdx` uses `foldedHist.forIdx` from PHR
- `rawTag` uses `foldedHist.forTag` from PHR
- no `commonHR` signal is connected into TAGE indexing/tag calculation

```scala
// Source: bpu/Bpu.scala:222-225
tage.io.fromMainBtb.result             := mbtb.io.result
tage.io.fromPhr.foldedPathHist         := phr.io.s0_foldedPhr
tage.io.fromPhr.foldedPathHistForTrain := phr.io.trainFoldedPhr
```

`commonHR` is a separate history structure (`ghr`/`bw`) maintained by `CommonHR`, and is consumed by modules such as SC, not by TAGE index/tag hash:

```scala
// Source: bpu/Bpu.scala:235
sc.io.commonHR := commonHR.io.s0_commonHR
```

### Index calculation summary

| Field | Source | Formula | History used |
|-------|--------|---------|--------------|
| `bankIdx` | PC[2:1] | direct extraction | None |
| `setIdx` | PC[11:3] XOR `forIdx` | `PC_setIdx ^ fold(PHR, min(histLen, 9))` | PHR folded to setIdx width |
| `rawTag` | PC[24:12] XOR `forTag` | `PC_tag ^ (fold13 ^ (fold12 << 1))` | PHR folded to 13/12 bits |
| `tag` (per branch) | `rawTag ^ cfiPosition` | rawTag XOR branch position in block | — |
| `useAltOnNaIdx` | cfiPC[7:1] | direct extraction from branch PC | None |

---

## Quality Checklist

- [x] Comply with order 1.1~1.8
- [x] specify memory depth/width/tables/banks/read/write ports
- [x] Includes memory entry code snippet (TageEntry, TageMetaEntry)
- [x] Completion of width/description table for each field
- Specify [x] pair BTB (mBTB) combining rules (position XOR tag, altOrBasePred)
- Write pseudocode including [x] paired BTB
- [x] latency/throughput quantification (2 cycle s0→s2, 1 pred-block/cycle)
- [x] Specify stage input/output timing (s0 SRAM req → s1 resp → s2 output)
- [x] Specify training trigger/FTQ storage/meta fields/port conflict processing
- [x] indexing/hashing method specified (PC bit layout, fold widths per table, per-branch tag XOR)

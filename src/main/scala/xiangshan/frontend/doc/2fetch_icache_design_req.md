# 2-Fetch ICache Design Request

- Based on: [icache_analysis.md](./icache_analysis.md)
- Target: 2 fetch bundles/cycle from MainPipe (2× input req BW)
- Date: 2026-04-22

---

## 1. Required Changes Overview

The baseline ICache has MainPipe receiving 1 fetch bundle/cycle from FTQ. The 2-Fetch design extends this to **2 fetch bundles/cycle**. This change cascades into the following problems, organized by priority.

### P1 — Blocking: must fix before 2-fetch is functionally correct

1. **DataArray bank conflict increase**: Concurrent accesses double, causing singlePort SRAM collisions to spike. Requires sub-bank interleaving (`NumInterleavedDataBank=2`). Without this, 2-bundle reads structurally conflict and correctness cannot be guaranteed. (→ §4.1)

2. **WayLookup dual-port correctness**: PrefetchPipe production and MainPipe consumption are both 2 entries/cycle. The real issue is preserving FIFO order, flush rollback, refill update, and exception entry correctness under dual enqueue/dequeue. A single ordering violation causes wrong instructions to be fetched. (→ §5.3)

3. **Arbitration complexity — FTQ/IFU interface and MainPipe control**: 2 bundle × (TLB hit/miss × Cache hit/miss) combinations; in-order IFU response guarantee; more FTQ backpressure cases. This is the core control path and must be defined before any other module can be implemented correctly. (→ §5.1, §5.5)

4. **Duplicate request merge required**: Duplicate handling has two levels. WayLookup/MainPipe-level merge is a hit-path optimization that avoids duplicate DataArray reads when two slots resolve to the same cacheline (`pTag + vSetIdx` match). MSHR-level merge is mandatory for miss-path correctness and uses the final physical cacheline address (`blkPAddr`) as the merge key. "Sequential adjacent" or "combined size ≤ 64B" are not sufficient merge criteria. (→ §5.1, §5.4)

5. **MissUnit MSHR shortage**: 2-bundle demand misses require up to 2 fetch MSHRs simultaneously. 4 MSHRs are insufficient — fetch MSHRs must double to 8. Prefetch MSHRs expanded to 20 to compensate prefetch BW. (→ §5.4)

6. **TLB / PMP path — 2× CAM hit path + PTW serialization**: iTLB and PMP hit paths are CAM-based and scale naturally to 2× by doubling comparator circuits. However, on TLB miss the PTW outbound channel remains 1-wide and must be serialized via a 2-entry pending buffer. Same-VPN dedup avoids duplicate PTW walks. (→ §5.2)

### P2 — Important: required for correct behavior under error and hazard conditions

1. **Refill/read hazard increase**: When a refill write and a MainPipe read target the same sub-bank in the same cycle, the priority and stall policy must be explicitly defined. Sub-bank interleaving reduces frequency; MSHR bypass handles most same-cycle cases. (→ §4.1)

2. **ECC / parity recovery complexity increase**: ECC errors can occur independently per bundle, requiring independent metaFlush ports (4 total), per-bundle refetch MSHR allocation, and correct exception ordering when both bundles have simultaneous errors. (→ §6.4)

3. **Replacer consistency**: The replacer operates on logical `vSetIdx` independently of the DataArray sub-bank split — no structural change is needed for victim selection. Touch serialization (up to 4 touches/cycle → serialize per even/odd replacer instance) must be handled. Victim collision is naturally prevented by `acquireArb` serialization. (→ §4.3)

### P3 — Optimization: address after P1/P2 are stable

1. **Refill completion serialization bottleneck**: Even with 8 fetch MSHRs, refill completion is 1/cycle. TileLink width increase to 512b/beat (1 beat per cacheline) halves fill latency and doubles effective MSHR throughput, partially mitigating this. (→ §5.4, §7.1)

2. **Prefetch pollution / fairness issue**: 20 prefetch MSHRs risk crowding out demand fetch or DCache traffic at L2. Dynamic throttle based on demand MSHR occupancy is needed. (→ §5.4)

3. **L2/L3 BW shortage**: ICache misses 2× + wider DCache stresses the entire memory hierarchy. Requires L2 bank doubling, MSHR scaling, and cross-team coordination — not an ICache-internal change. (→ §7)

4. **Sub-bank- and way-parallel refill and fetch**: In the baseline, any refill write globally blocks all DataArray reads (`read.ready := !write.valid`). Two independent dimensions of parallelism become available with the 2-fetch structure:

   - **Sub-bank dimension**: With `NumInterleavedDataBank=2`, refill and fetch targeting different sub-banks (`vSetIdx[0]` differs) can proceed in the same cycle. The arbitration must be made per-sub-bank rather than global. This is the common case for sequential access patterns.
   - **Way dimension**: Each way inside a DataBank is a separate `SRAMTemplate` instance. The current arbitration (`bank.read.ready := !bank.write.valid`) blocks all way reads whenever any write is valid, even if the write and read target different ways. Switching to per-way arbitration (`way[i].read.ready := !way[i].write.valid`) allows a refill to way W and a fetch from way W′ (W ≠ W′) to proceed in parallel within the same sub-bank.

   The two dimensions are orthogonal. Combining them means a refill is only a stall when it writes to the exact same (sub-bank, way) as the concurrent fetch read — which is rare in practice. (→ §4.1)

---

In short, 2-Fetch ICache is not merely a matter of doubling the MainPipe input BW.
It is a structural change requiring co-redesign of DataArray / WayLookup / MissUnit / PrefetchPipe as well as the FTQ-IFU contract, replacer correctness, L2 admission policy, and verification strategy.

---

## 2. Design Delta Summary

| Item | Baseline | 2-Fetch |
| --- | --- | --- |
| MainPipe fetch bundles/cycle | 1 | **2** |
| DataArray NumInterleavedDataBank | 1 (none) | **2** |
| MetaArray NumInterleavedBank | 2 | **4** |
| NumFetchMshr | 4 | **8** |
| NumPrefetchMshr | 10 | **20** |
| WayLookupSize | 32 | **64** |
| WayLookup read ports/cycle | 1 | **2** |
| WayLookup write ports/cycle | 1 | **2** |
| WayLookup exceptionEntry | 1 (global) | **2 (per slot)** |
| PrefetchPipe MetaArray reads/cycle | 2 (PortNumber) | **4** |
| iTLB lookup ports/cycle | 1 | **2** |
| PMP check ports/cycle | 1 | **2** |
| Replacer touch ports/cycle | 2 (PortNumber) | **4 (2 bundle × PortNumber)** |
| MissUnit duplicate merge logic | none | **new required** |
| TileLink data channel width (L1→L2) | 256b/beat | **512b/beat** |
| FTQ fetchReq ports | 1 | **2** |
| IFU resp ports | 1 | **2** |

---

## 3. Key Parameter Changes

```text
NumFetchBundles      = 2          // was 1
NumFetchMshr         = 8          // was 4
NumPrefetchMshr      = 20         // was 10
WayLookupSize        = 64         // was 32
NumInterleavedDataBank = 2        // new
NumInterleavedMetaBank = 4        // was 2
```

`PortNumber=2`, `nSets=256`, `nWays=4`, `blockBytes=64`, `DataBanks=8` remain unchanged.

### PortNumber=2 vs NumFetchBundles=2

These represent different axes and must not be conflated.

| Parameter | Meaning |
| --- | --- |
| `PortNumber=2` | Within-bundle doubleline: simultaneous access when one fetch req spans 2 consecutive cachelines |
| `NumFetchBundles=2` | Between-bundle: number of independent fetch requests MainPipe processes simultaneously from FTQ |

Worst case: `NumFetchBundles=2` × `PortNumber=2` = **4 simultaneous cacheline accesses**.

```text
// Recommended naming convention
s0_bundle[0..1]               // bundle axis (NumFetchBundles)
s0_bundle[b].port[0..1]       // port axis within bundle (PortNumber)
s0_bundle[b].port[p].vSetIdx  // up to 4 independent vSetIdx values
```

---

## 4. Memory Organization

### 4.1 DataArray — 2-Way Set-Interleaved Design (P1.1, P2.1, P3.4)

#### Problem

The baseline DataArray is `SRAMTemplate(set=256, singlePort=true)` × 8 banks × 4 ways. When 2 fetch bundles simultaneously access the same DataBank SRAM, a conflict occurs. Worst case with doubleline: 2 bundles × 2 ports = 4 concurrent cacheline accesses.

#### Sub-Bank Interleaving Design

```text
NumInterleavedDataSet = nSets / NumInterleavedDataBank = 256 / 2 = 128 sets/sub-bank
sub_bank_sel = vSetIdx[0]
sub_row_addr = vSetIdx[7:1]

DataSubArray[vSetIdx[0]][data_bank_idx][way].read(vSetIdx[7:1], waymask)
```

| | Baseline | 2-Fetch |
| --- | --- | --- |
| Sub-bank count | 1 | 2 |
| DataBanks per sub-bank | 8 | 8 |
| Ways per sub-bank | 4 | 4 |
| SRAM depth per sub-bank | 256 sets | 128 sets |
| Total SRAM instances | 32 | 64 |
| Total SRAM bits | 32×256×66b | 64×128×66b (same) |

#### Refill/Read Hazard Policy

Priority when a refill write and a MainPipe read enter the same sub-bank in the same cycle:

```text
// Per-sub-bank independent arbitration
for sb in 0..1:
  subbank[sb].read.ready  := !subbank[sb].write.valid
  subbank[sb].write.ready := true  // refill always wins

// Case A: refill → sb[1], fetch bundle 1 → sb[1]
//   → bundle 1 stall 1 cycle; bundle 0 (→ sb[0]) can continue

// Case B: refill → sb[1], fetch bundle → sb[0]
//   → no stall, fully parallel

// Case C: fetch hits same set immediately after refill completes
//   → MSHR bypass: use MSHR data directly instead of DataArray read
val useBypass_b = missResp.valid
                  && (missResp.bits.vSetIdx === s1_vSetIdx_b)
                  && (missResp.bits.blkPAddr === s1_blkPAddr_b)
s1_data_b := Mux(useBypass_b, missResp.bits.data, dataArray_resp_b)
```

#### Per-Way Parallel Access Optimization (P3.4)

Two orthogonal dimensions of parallelism are available:

**Sub-bank dimension**: Refill and fetch targeting different sub-banks (`vSetIdx[0]` differs) proceed in the same cycle. The per-sub-bank arbitration above handles this.

**Way dimension**: Each way inside a DataBank is a separate `SRAMTemplate` instance. The baseline arbitration globally blocks all way reads on any write valid:

```text
// Baseline (conservative)
bank.read.ready := !bank.write.valid  // any write blocks all reads

// Optimized (per-way)
way[i].read.ready := !way[i].write.valid  // only block the way being written
```

Since refill always targets exactly one way (determined by victim selection) and fetch reads exactly one way (from WayLookup waymask), if those ways differ the read and write can proceed simultaneously in the same sub-bank.

The two dimensions are orthogonal. A stall occurs only when refill and fetch target the exact same (sub-bank, way) — which requires the fetch to hit the cacheline currently being refilled into the same way, an extremely rare case.

---

### 4.2 MetaArray — Quad-Interleaved Design

A 2-wide PrefetchPipe requires up to 4 MetaArray reads per cycle: 2 prefetch requests × `PortNumber=2` = 4 reads. The baseline `NumInterleavedBank=2` supports only 2 reads/cycle.

```text
NumInterleavedBank = 4, NumInterleavedSet = nSets / 4 = 64 sets/bank

MetaBank 0: sets 0,4,8,...   (set%4==0)
MetaBank 1: sets 1,5,9,...   (set%4==1)
MetaBank 2: sets 2,6,10,...  (set%4==2)
MetaBank 3: sets 3,7,11,...  (set%4==3)
```

Port priority (independent per bank): `flushAll` > `flush.req` (ECC) > `write.req` (refill) > `read.req` (PrefetchPipe). A refill write blocks only the affected bank; prefetch reads on other banks continue.

---

### 4.3 Replacer — Touch Serialization and Victim Selection (P2.3)

The replacer operates on logical `vSetIdx` independently of the DataArray sub-bank split — **no structural change is needed for victim selection**. The replacer's 2-instance even/odd split and `acquireArb` serialization continue to prevent victim collision without additional logic.

The only required change is touch serialization. In 2-fetch, up to 4 touches arrive per cycle (2 bundles × `PortNumber=2`). Each replacer instance handles at most 1 touch/cycle.

```text
// Touch conflict detection
val touch_even = Seq(b0_p0, b0_p1, b1_p0, b1_p1).filter(_.vSetIdx(0) === 0.U)
val touch_odd  = Seq(b0_p0, b0_p1, b1_p0, b1_p1).filter(_.vSetIdx(0) === 1.U)

replacers(0).touch(touch_even.head)  // apply only the first; rest dropped
replacers(1).touch(touch_odd.head)
```

One-cycle LRU staleness from dropped touches has negligible impact on eviction policy.

**Victim selection**: `acquireArb` is a single `Arbiter(8+1)`, so only one acquire fires per cycle. `replacer.victim.req` therefore fires at most once per cycle, and the replacer's next-cycle touch completes before the next acquire can fire. No additional victim arbitration logic is needed.

---

## 5. Pipeline Architecture

### 5.1 MainPipe — Dual-Bundle Control (P1.3, P1.4)

#### Interface Changes

```scala
// Baseline
val req  : Decoupled[FtqFetchRequest]
val resp : Valid[ICacheRespBundle]

// 2-Fetch
val req  : Vec[NumFetchBundles, Decoupled[FtqFetchRequest]]
val resp : Vec[NumFetchBundles, Valid[ICacheRespBundle]]
```

#### Duplicate Detection and Merge Levels

Duplicate handling must be split into two levels because the earliest safe information differs by pipeline stage.

**WayLookup/MainPipe-level merge** is a hit-path optimization. At this point the translated tag and set index are already available from WayLookup, so the same-cacheline test can be:

```text
same_line_hit_path =
  slot0.pTag   == slot1.pTag &&
  slot0.vSetIdx == slot1.vSetIdx
```

When `same_line_hit_path` is true, MainPipe can issue one DataArray read and fan out the returned line data to both bundle consumers. This reduces DataArray bank conflicts and dynamic power. However, this optimization requires per-consumer tracking because a merged WayLookup line may have two independent FTQ consumers.

Required consumer metadata:

```text
MergedLineConsumer:
  ftqIdx
  fetchSlot
  byteRange
  valid
  flushed
```

Flush handling:

- If slot 0 is flushed, slot 1 is also flushed because slot 1 is younger in the same 2-fetch group.
- If only slot 1 is flushed, remove/clear only the slot 1 consumer and keep the slot 0 consumer.
- If a BPU redirect targets an older FTQ entry, all consumers younger than the redirect are cleared.

**MSHR-level merge** is mandatory for miss-path correctness and resource control. It must use the final physical cacheline address:

```text
same_line_miss_path = slot0.blkPAddr == slot1.blkPAddr
```

MSHR merge is still required even if WayLookup/MainPipe merge is implemented, because new misses can also match already outstanding MSHRs from older requests or prefetches.

| Case | Behavior |
| --- | --- |
| Same-line hit path | One DataArray read; both consumers receive slices from the same returned line |
| Same-line miss path | One MSHR allocation; all consumers are registered as waiters |
| Existing MSHR match | Do not allocate a new MSHR; attach the demand consumer to the existing MSHR |
| Prefetch MSHR hit by demand | Attach demand consumer and promote priority to demand |

Implementation recommendation:

- v1 must implement MSHR-level merge.
- WayLookup/MainPipe-level merge should be implemented if timing and flush consumer tracking are acceptable.
- If WayLookup/MainPipe merge is deferred, correctness remains intact, but same-line hit cases may perform duplicate DataArray reads.

#### S0 Advance Conditions

```text
b1NeedsDataRead  = !same_line_hit_path
b1ConsumerReady  = !same_line_hit_path || mergeConsumerAllocated

s0_canGo[0] = toData[0].ready && fromWayLookup.slot0.ready && s1_ready[0]
s0_canGo[1] = fromWayLookup.slot0.ready && fromWayLookup.slot1.ready && s1_ready[1]
              && b1ConsumerReady
              && (!b1NeedsDataRead || (toData[1].ready && !subbank_conflict))
fromFtq[0].ready = s0_canGo[0]
fromFtq[1].ready = s0_canGo[0] && s0_canGo[1]
```

For a same-line hit-path merge, slot 1 must still be accepted as a consumer. It should not be stalled simply because it does not need a separate DataArray read.

#### S1 In-Order Response Policy

| Bundle 0 | Bundle 1 | IFU Response |
| --- | --- | --- |
| Hit | Hit | Both simultaneously |
| Hit | Miss | B0 now; B1 waits for MSHR |
| Miss | Hit | B1 saved in `s1_b1_resp_buf`; respond after B0 MSHR completes |
| Miss | Miss | Each allocated an MSHR (or duplicate merge) |

```scala
// B0 miss, B1 hit: save B1 result
when(bundle0_miss && bundle1_hit):
  s1_b1_resp_buf       := bundle1_resp
  s1_b1_resp_buf_valid := true

// On B0 MSHR completion: flush both
when(bundle0_missResp.valid && missMatch_0):
  IFU.resp[0].valid := true
  IFU.resp[1].valid := s1_b1_resp_buf_valid
  s1_b1_resp_buf_valid := false
```

#### Flush Policy

```text
// Flushing bundle 0 also flushes bundle 1 (B1 is always a later FTQ entry)
when(s1_flush_0): { s1_valid[0] := false; s1_valid[1] := false; s1_b1_resp_buf_valid := false }
when(s1_flush_1 && !s1_flush_0): { s1_valid[1] := false; s1_b1_resp_buf_valid := false }
```

---

### 5.2 PrefetchPipe — 2-Wide + PTW Serialization (P1.6)

The PrefetchPipe is extended to fully 2-wide. iTLB and PMP are CAM-based, so the hit path scales to 2× by simply doubling comparator circuits — no structural change. On TLB miss, the PTW outbound channel remains 1-wide, but a TLB miss in slot 0 must not block a TLB hit in slot 1 from completing its tag lookup and WayLookup enqueue.

Policy:

- Each slot has independent TLB-miss progress and independent WayLookup completion.
- Slot 1 may enqueue before slot 0 when slot 0 is waiting for PTW and slot 1 is TLB-hit/cache-classified.
- WayLookup must therefore provide an **ordered consumer view** to MainPipe rather than assuming physical FIFO enqueue order equals FTQ order.
- MainPipe may consume slot 1 only when the older slot 0 entry is also available or known to be an exception/redirect case.

This preserves 2-wide prefetch throughput during slot 0 TLB misses while keeping the external fetch response ordered.

#### PTW Pending Buffer

```scala
ptw_pending: Vec[2, Valid[PTWReqEntry]]

// Simultaneous miss
when(slot0_tlb_miss && slot1_tlb_miss):
  val same_vpn = (slot0_vpn === slot1_vpn)
  ptw_pending[0] := {vpn=slot0_vpn, id=0}
  when(!same_vpn): ptw_pending[1] := {vpn=slot1_vpn, id=1}
  // same VPN: single PTW req, update both slots on response

// 1-wide PTW channel: head-of-queue priority
io.ptw_req.valid := ptw_pending[0].valid
when(io.ptw_req.fire): ptw_pending[0] := ptw_pending[1]; ptw_pending[1].valid := false

// Response routing by VPN match
when(io.ptw_resp.valid):
  when(resp.vpn === slot0_pending_vpn): slot0_tlb_update := true
  when(resp.vpn === slot1_pending_vpn): slot1_tlb_update := true
```

WayLookup enqueue policy: slot 1 can enqueue before slot 0. Ordering is restored by storing slot identity and FTQ order metadata in WayLookup and presenting ordered entries to MainPipe.

| Slot 0 TLB | Slot 1 TLB | WayLookup enqueue |
| --- | --- | --- |
| hit | hit | simultaneous |
| hit | miss | slot 0 immediately; slot 1 after PTW |
| miss | hit | slot 1 enqueues early; slot 0 enqueues after PTW |
| miss | miss (diff VPN) | each slot enqueues when its PTW resolves; PTW issue remains 1-wide |
| miss | miss (same VPN) | 1 PTW req; both update on response and can enqueue together |

Required WayLookup metadata for early slot 1 enqueue:

```text
WayLookupEntry:
  ftqIdx
  fetchSlot        // 0 or 1 within the 2-fetch group
  groupSeq         // monotonically increasing 2-fetch group id, or equivalent order tag
  ready
  exception
  pTag / waymask / meta
```

MainPipe ordered read rule:

```text
oldest0 = entry(groupSeq = readGroup, fetchSlot = 0)
oldest1 = entry(groupSeq = readGroup, fetchSlot = 1)

read[0].valid := oldest0.ready
read[1].valid := oldest0.ready && oldest1.ready
```

If slot 1 is ready before slot 0, it remains stored in WayLookup but is not exposed to MainPipe as `read[1]` until slot 0 becomes ready or slot 0 triggers a redirect/exception path.

---

### 5.3 WayLookup — Dual-Port Correctness and Ordered View (P1.2)

#### Structure

```text
entries:     RegInit(VecInit.fill(WayLookupSize=64)(...))
write side:  accepts up to 2 completed PrefetchPipe slots/cycle
read side:   exposes up to 2 ordered MainPipe entries/cycle

entry key = (groupSeq, fetchSlot)
```

Because PrefetchPipe allows slot 1 to enqueue before slot 0, WayLookup cannot rely on raw FIFO insertion order alone. It must track readiness per ordered slot and present MainPipe with the oldest group in order.

```text
group[readGroup].slot[0].ready
group[readGroup].slot[1].ready

io.read[0].valid := slot0.ready && !updateStall(slot0)
io.read[1].valid := slot0.ready && slot1.ready && !updateStall(slot1)
```

Physical implementation can still use a circular buffer, but the buffer must either:

- reserve two entries per accepted 2-fetch group and allow out-of-order fill of slot 0/1, or
- use associative lookup by `(groupSeq, fetchSlot)` for the head group.

The first option is preferred for timing.

#### Exception Entry — Per-Slot

Baseline has 1 global `exceptionEntry`. In 2-Fetch, each slot holds its exception independently:

```scala
exceptionEntry: Vec[2, Reg[Valid[WayLookupExceptionEntry]]]
// PrefetchPipe slot 0 exception → exceptionEntry[0]
// PrefetchPipe slot 1 exception → exceptionEntry[1]
// Flush: clear both
```

If slot 0 has an exception, slot 1 response is held until IFU redirects after slot 0.

#### BPU Flush Rollback

```text
val flushTargetPtr = first entry in WayLookup where ftqIdx == flush.ftqIdx
writePtr := flushTargetPtr
// flush targets slot 0 ftqIdx → rollback both slot 0 and slot 1
// flush targets slot 1 ftqIdx only → keep slot 0, rollback slot 1
```

#### updateStall — Simultaneous 2-Slot Check

```text
val updateStall_0 = entryUpdate(headGroup.slot0)
val updateStall_1 = entryUpdate(headGroup.slot1)
io.read[0].valid := headGroup.slot0.ready && !updateStall_0
io.read[1].valid := headGroup.slot0.ready && headGroup.slot1.ready && !updateStall_1
// if only B1 has updateStall, B0 can still proceed
```

Pre-compute `updateStall[i]` 1 cycle ahead and store in register to shorten S0 critical path.

---

### 5.4 MissUnit — MSHR Scale-Up + Duplicate Merge (P1.4, P1.5, P3.1, P3.2)

#### MSHR Scale-Up

```text
fetchMSHRs    : 4 → 8    (Arbiter(8+1))
prefetchMSHRs : 10 → 20
```

#### MSHR-Level Duplicate Merge

```scala
val same_cacheline = (bundle0.blkPAddr === bundle1.blkPAddr) && bundle0_miss && bundle1_miss

when(same_cacheline):
  // allocate 1 MSHR for bundle 0, register bundle 1 as waiter
  fetchMSHR[n].waiters += bundle1
  // broadcast to both on refill completion
.otherwise:
  // allocate independent MSHRs for bundle 0 and bundle 1
```

The MSHR merge key is the physical cacheline address and must be checked against both:

- the other slot in the same cycle
- all already allocated fetch/prefetch MSHRs

Required behavior:

```text
if demand miss matches existing fetch MSHR:
    attach demand consumer to existing fetch MSHR
else if demand miss matches existing prefetch MSHR:
    attach demand consumer
    promote entry priority/class to demand
else if slot0 and slot1 miss same blkPAddr:
    allocate one fetch MSHR
    attach both slot consumers
else:
    allocate independent fetch MSHRs if credits are available
```

WayLookup/MainPipe-level merge is not sufficient for miss correctness because it only sees the currently paired slots. MSHR-level merge is the final guard against duplicate outstanding physical-line requests.

#### L2 Acquire Priority

```text
acquireArb (Arbiter(8+1)):
  0~7: fetchMSHR[0..7]   — highest priority (demand)
  8:   prefetch bundle    — lower priority
```

`acquireArb` is a single arbiter, so only one acquire fires per cycle. This serializes `replacer.victim.req` and prevents victim collision without additional logic.

#### Prefetch Throttle (P3.2)

```scala
val activeFetchMSHR   = PopCount(fetchMSHRs.map(_.valid))
val maxPrefetchActive = Mux(activeFetchMSHR >= 4, 8.U, 20.U)
val prefetchBlocked   = PopCount(prefetchMSHRs.map(_.valid)) >= maxPrefetchActive
prefetchMSHR.io.alloc.valid := s2_prefetch_req.valid && !prefetchBlocked
```

#### Refill Completion Serialization (P3.1)

TileLink width increase to 512b/beat reduces the per-cacheline fill from 2 beats to 1 beat, halving fill latency and doubling effective MSHR throughput. This is the primary mitigation for the 1/cycle refill completion constraint.

---

### 5.5 FTQ / IFU Interface Contract (P1.3)

#### FTQ → ICache

```scala
// 2-Fetch
val fetchReq   : Vec[NumFetchBundles, Decoupled[FtqFetchRequest]]
val prefetchReq: Vec[NumFetchBundles, Decoupled[PrefetchRequest]]
// fetchReq[1].valid only when fetchReq[0].valid
```

#### ICache → IFU

```scala
val resp: Vec[NumFetchBundles, Valid[ICacheRespBundle]]
// resp[1].valid → resp[0].valid (in-order guarantee)
// resp[1] always corresponds to FTQ entry[ptr+1]
```

IFU must handle 2× wider instruction alignment, predecode (RVC), and decode bus. `fromIfu.respStall` holds both responses when asserted.

---

## 6. ECC and Error Handling

### 6.1 ECC / Parity Recovery — 2-Bundle Handling (P2.2)

#### metaFlush Port Expansion

```scala
// Baseline: PortNumber=2 ports
val metaFlush: Vec[PortNumber, Valid[MetaFlushBundle]]

// 2-Fetch: NumFetchBundles × PortNumber = 4 ports
val metaFlush: Vec[NumFetchBundles * PortNumber, Valid[MetaFlushBundle]]
// [0,1] = bundle 0 port 0,1 ; [2,3] = bundle 1 port 0,1
```

With `NumInterleavedMetaBank=4`, up to 4 simultaneous flushes can be processed if each targets a different bank. When 2 flushes arrive at the same bank, B0 wins.

#### 2-Bundle ECC Policy

| Bundle 0 | Bundle 1 | Behavior |
| --- | --- | --- |
| ECC error (refetch) | miss | B0 issues refetch req first; B1 issues miss req independently |
| ECC error (refetch) | ECC error (refetch) | B0 first; B1 next cycle (2 MSHRs needed) |
| ECC exception (`EnableCorruptRefetch=false`) | valid | B0 exception resp to IFU; B1 held |

---

## 7. L2/L3 Bandwidth (P3.3, P3.1)

### 7.1 TileLink L1→L2 Width Increase (highest priority)

```text
Baseline: 256b/beat × 2 beats = 512b per cacheline fill
2-Fetch:  512b/beat × 1 beat  = 512b per cacheline fill

Effect:
  - Fill latency halved → shorter MSHR occupancy
  - Effective fetch MSHR throughput doubled
```

### 7.2 L2 Scale-Up

```text
L2 banks:  4 → 8   (2× internal throughput for simultaneous ICache + DCache service)
L2 MSHRs:  N → 2N  (handles L1 fetchMSHR=8 + DCache demand simultaneously)
```

### 7.3 Prefetch Admission Control at L2

L2-side throttle to prevent prefetchMSHR=20 from monopolizing bandwidth:

1. Separate priority bit for prefetch requests in TileLink user field
2. Throttle prefetch when L2 BW utilization > 80%
3. Prefetch-filled L2 entries get lower eviction priority than demand entries
4. L2 prefetch filter: block duplicates already in L2

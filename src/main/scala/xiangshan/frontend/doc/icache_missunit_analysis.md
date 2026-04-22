# ICache MissUnit 내부 구조 분석

관련 파일:

- `src/main/scala/xiangshan/frontend/icache/ICacheMissUnit.scala`
- `src/main/scala/xiangshan/frontend/icache/ICacheMshr.scala`
- `src/main/scala/xiangshan/frontend/icache/ICacheReplacer.scala`
- `src/main/scala/xiangshan/frontend/icache/Utils.scala`
- `src/main/scala/xiangshan/frontend/icache/Bundles.scala`

---

## 1. 역할 요약

`ICacheMissUnit`은 ICache에서 miss가 발생한 요청을 받아 L2에 TileLink Get을 발행하고, Grant beat를 수집한 뒤 SRAM과 파이프라인에 결과를 돌려주는 블록이다.

진입점은 두 개다.

- `fetchReq`: main pipe에서 오는 fetch miss (실제 IFU 응답을 위한 요청)
- `prefetchReq`: prefetch pipe에서 오는 prefetch miss (선행 refill 요청)

fetch가 우선순위가 더 높다. TileLink acquire 단계에서도 fetch MSHR가 prefetch보다 먼저 선택된다.

---

## 2. 전체 구조

```text
                            ┌─────────────────────────────────────────────────────┐
                            │                  ICacheMissUnit                     │
 fetchReq ──► fetchDemux ──►│ fetchMSHR[0] ──┐                                   │
                            │ fetchMSHR[1] ──┤                                   │
                            │ fetchMSHR[2] ──┤ acquireArb (in[0..3]) ──► memAcquire
                            │ fetchMSHR[3] ──┘                           (TileLink A)
                            │                ▲                                   │
                            │                │                                   │
prefetchReq ►prefetchDemux─►│ prefMSHR[0]──┐ │                                   │
                            │ prefMSHR[1]──┤ │                                   │
                            │    ...       ├─► prefetchArb──► acquireArb(in[4])  │
                            │ prefMSHR[9]──┘                                     │
                            │                                                    │
                            │  priorityFIFO (sel for prefetchArb)                │
                            │                                                    │
                            │  memGrant (TileLink D) ──► beat 수집               │
                            │                            │                       │
                            │                            ▼                       │
                            │                      writeSramValid?               │
                            │                      ├─► metaWrite (MetaArray)     │
                            │                      └─► dataWrite (DataArray)     │
                            │                                                    │
                            │                      io.resp (broadcast)           │
                            │                      ├─► MainPipe                  │
                            │                      ├─► PrefetchPipe              │
                            │                      └─► WayLookup                 │
                            └─────────────────────────────────────────────────────┘
```

---

## 3. IO 포트 목록

| 포트 | 방향 | 타입 | 설명 |
| --- | --- | --- | --- |
| `fetchReq` | input | `DecoupledIO[MissReqBundle]` | main pipe의 fetch miss 요청 |
| `prefetchReq` | input | `DecoupledIO[MissReqBundle]` | prefetch pipe의 prefetch miss 요청 |
| `resp` | output | `Valid[MissRespBundle]` | refill 완료 broadcast (단일 포트) |
| `metaWrite` | output | `MetaWriteBundle` | MetaArray 쓰기 |
| `dataWrite` | output | `DataWriteBundle` | DataArray 쓰기 |
| `victim` | output | `ReplacerVictimBundle` | Replacer에 victim way 요청/응답 |
| `memAcquire` | output | `DecoupledIO[TLBundleA]` | TileLink A 채널 (L2 요청) |
| `memGrant` | input | `DecoupledIO[TLBundleD]` | TileLink D 채널 (L2 응답) |
| `fencei` | input | `Bool` | 전체 flush, 진행 중 MSHR도 취소 |
| `flush` | input | `Bool` | prefetch MSHR만 취소 |
| `wfi` | 양방향 | `WfiReqBundle` | WFI 안전 진입 조건 제공 |

### MissReqBundle 필드

```scala
class MissReqBundle {
  val blkPAddr: UInt  // cache line 단위 physical address (하위 blockOffBits 제거)
  val vSetIdx:  UInt  // set index (virtual address 기반)
}
```

### MissRespBundle 필드

```scala
class MissRespBundle {
  val blkPAddr:    UInt  // refill된 block 주소
  val vSetIdx:     UInt  // 채워 넣은 set
  val waymask:     UInt  // 선택된 victim way (one-hot)
  val data:        UInt  // cache line 전체 데이터 (blockBits)
  val maybeRvcMap: UInt  // RVC 압축 명령 힌트 (MaxInstNumPerBlock bits)
  val corrupt:     Bool  // TileLink corrupt 응답 여부
  val denied:      Bool  // TileLink denied 응답 여부
}
```

---

## 4. 핵심 서브모듈

### 4.1 DeMultiplexer — 요청 분산기

```scala
class DeMultiplexer[T <: Data](gen: T, n: Int)
// in: 1개의 incoming request
// out: n개의 MSHR 슬롯 중 비어 있는 것으로 전달
// chosen: 선택된 슬롯 번호 (PriorityEncoder 기준 — 낮은 번호 우선)
```

`fetchDemux`는 `NumFetchMshr = 4`개로, `prefetchDemux`는 `NumPrefetchMshr = 10`개로 요청을 분산한다.

동작 방식은 우선순위 인코더다. `out(i).ready`가 true인 슬롯 중 가장 낮은 번호로 전달된다.

### 4.2 ICacheMshr — 개별 miss 처리 슬롯

MSHR 하나가 담당하는 일:

1. miss 요청 등록 (`blkPAddr`, `vSetIdx` 저장)
2. TileLink Get 발행 (acquire)
3. victim way 수신 및 저장
4. lookup 요청에 대해 동일 block 여부 응답
5. Grant 완료 후 invalid 신호로 슬롯 해제

각 MSHR는 세 개의 레지스터로 상태를 표현한다.

| 레지스터 | 의미 |
| --- | --- |
| `valid` | 이 슬롯이 살아 있음 |
| `issue` | TileLink acquire를 이미 발행했음 |
| `flush` / `fencei` | flush 또는 fencei가 왔음 — SRAM write와 resp를 억제함 |

#### MSHR 상태 전이

```text
[비어 있음: valid=false]
    │
    │ req.fire
    ▼
[등록됨: valid=true, issue=false]
    │
    │ acquire.fire (L2에 Get 발행)
    ▼
[발행됨: valid=true, issue=true]
    │                │
    │ invalid        │ fencei/flush (issue 후)
    │ (lastFireNext) │   → flush/fencei 레지스터만 set
    ▼                │   → valid는 유지 (L2 응답까지 기다림)
[비어 있음]           ▼
              [flush 마킹: valid=true, issue=true, flush=true]
                  │
                  │ invalid (lastFireNext)
                  ▼
              [비어 있음]
```

fencei/flush가 오기 전에 아직 acquire를 안 보냈다면 (`!issue`), `valid := false`로 즉시 해제된다.  
acquire를 이미 보냈다면 L2 응답이 올 때까지 슬롯을 유지하되 `flush = true`를 마킹해 응답 이후 SRAM write와 resp를 억제한다.

#### acquire 조건

```scala
io.acquire.valid := valid && !issue && !io.flush && !io.fencei && !io.wfi.wfiReq
```

WFI 요청이 있으면 새 L2 요청을 내보내지 않는다.

#### lookup 조건

```scala
val hit = valid && !fencei && !flush &&
          (lookup.req.bits.vSetIdx === vSetIdx) &&
          (lookup.req.bits.blkPAddr === blkPAddr)
```

같은 사이클에 combinational하게 응답한다. fencei/flush가 마킹되어 있으면 hit로 판정하지 않는다.

#### info 포트

Grant 응답 수집 후 SRAM write와 resp 생성에 필요한 정보를 제공한다.

```scala
io.info.valid         := valid && !flush && !fencei
io.info.bits.blkPAddr := blkPAddr
io.info.bits.vSetIdx  := vSetIdx
io.info.bits.way      := way  // acquire.fire 시 victimWay에서 래치됨
```

### 4.3 FIFOReg — prefetch 발행 순서 보장

```scala
private val priorityFIFO = Module(new FIFOReg(UInt(log2Ceil(NumPrefetchMshr).W), NumPrefetchMshr, hasFlush = true))
```

prefetch MSHR는 10개가 있으나, 먼저 등록된 것이 먼저 L2 acquire를 나가야 한다.  
`prefetchDemux.io.chosen` (등록된 슬롯 번호)을 FIFO에 넣고, 꺼낼 때 이 번호로 `MuxBundle`을 선택해 순서를 보장한다.

`flush` 또는 `fencei` 시 FIFO 자체도 flush된다.

### 4.4 MuxBundle — sel 기반 N-to-1 선택기

```scala
class MuxBundle[T <: Data](gen: T, n: Int)
// in(i): i번 prefetch MSHR의 acquire
// sel: FIFO에서 꺼낸 번호
// out: sel번 MSHR의 acquire를 내보냄
```

선택은 combinational이다. sel에 해당하는 `in(i).valid`만 `out.valid`로 전달되고, 나머지 `in`의 ready는 0이 된다.

### 4.5 Arbiter — fetch vs prefetch 최종 중재

```scala
private val acquireArb = Module(new Arbiter(new MshrAcquireBundle(edge), NumFetchMshr + 1))
// in(0..3): fetchMSHR[0..3]의 acquire
// in(4)   : prefetchArb.out (prefetch 대표)
// out     : memAcquire (TileLink A)
```

표준 Chisel Arbiter이므로 낮은 인덱스가 우선이다.  
fetch MSHR 4개(인덱스 0~3)가 prefetch(인덱스 4)보다 항상 먼저 선택된다.

---

## 5. Duplicate Miss 차단

새 miss 요청이 들어오면 모든 MSHR를 동시에 lookup해 동일 block이 이미 outstanding인지 확인한다.

```scala
fetchHit    := allMshr.map(_.io.lookUps(0).resp.hit).reduce(_ || _)
prefetchHit := allMshr.map(_.io.lookUps(1).resp.hit).reduce(_ || _) || prefetchHitFetchReq
```

`prefetchHitFetchReq`는 같은 사이클에 들어온 fetch 요청과 prefetch 요청이 같은 block을 가리키는 경우를 잡는다.

중복이 감지되면:

```scala
fetchDemux.io.in.valid    := io.fetchReq.valid && !fetchHit
io.fetchReq.ready         := fetchDemux.io.in.ready || fetchHit
```

`fetchHit = true`이면 새 MSHR에 등록하지 않고 요청을 소모(`ready = true`)한다.  
이미 같은 block을 처리하는 MSHR가 있으므로 그 Grant broadcast를 기다리면 된다.

---

## 6. TileLink D 채널 (Grant) 수집

```text
memGrant beat 0 → respDataReg(0)
memGrant beat 1 → respDataReg(1)    (refillCycles = 2, 64B / 32B bus = 2 beats)
  └── lastFire (beat 1이 완료되는 시점)
```

`readBeatCnt`가 `refillCycles - 1`에 달하는 beat를 `waitLast`로 표시하고,  
`waitLast && io.memGrant.fire`를 `lastFire`로 정의한다.

corrupt/denied는 beat 단위로 누적한다.

```scala
corruptReg := corruptReg || io.memGrant.bits.corrupt
deniedReg  := deniedReg  || io.memGrant.bits.denied
```

어느 beat에서라도 corrupt/denied가 오면 최종적으로 set된다.

Grant 완료 타이밍은 다음과 같다.

```text
lastFire    (사이클 N)   : beat 수집 완료, mshrInfo 래치
lastFireNext(사이클 N+1) : SRAM write + resp broadcast 발행, MSHR invalid
```

`mshrInfo`를 1 사이클 미리 래치하는 이유는 timing 개선이다.

---

## 7. MSHR 해제 타이밍

```scala
(0 until NumAllMshr).foreach(i => allMshr(i).io.invalid := lastFireNext && (idNext === i.U))
```

`idNext`는 Grant source ID를 1 사이클 래치한 것이다.  
`lastFireNext` 시점에 해당 source ID와 일치하는 MSHR 하나만 `invalid = true`로 해제된다.

---

## 8. SRAM 쓰기와 응답 broadcast

### 8.1 writeSramValid 조건

```scala
private val writeSramValid = respValid && !corruptReg && !io.flush && !io.fencei
```

| 조건 | 의미 |
| --- | --- |
| `respValid` | `mshrValid && lastFireNext` |
| `!corruptReg` | L2 응답이 정상 (TileLink spec: denied는 corrupt를 내포하므로 별도 체크 불필요) |
| `!io.flush` | prefetch flush가 없음 |
| `!io.fencei` | fence.i가 없음 |

corrupt/denied이거나 flush/fencei가 온 경우 SRAM write를 생략한다.

### 8.2 MetaArray 쓰기

```scala
io.metaWrite.req.bits.generate(
  phyTag     = getPTagFromBlk(mshrInfo.blkPAddr),
  maybeRvcMap = maybeRvcMap,
  vSetIdx    = mshrInfo.vSetIdx,
  waymask    = waymask,
  poison     = false.B
)
io.metaWrite.req.valid := writeSramValid
```

### 8.3 DataArray 쓰기

```scala
io.dataWrite.req.bits.generate(
  data    = respDataReg.asUInt,
  vSetIdx = mshrInfo.vSetIdx,
  waymask = waymask,
  poison  = false.B
)
io.dataWrite.req.valid := writeSramValid
```

### 8.4 응답 broadcast

```scala
private val respValid = mshrValid && lastFireNext
io.resp.valid := respValid
```

주의: flush/fencei 중이더라도 `mshrValid = false`가 되어 `respValid = false`가 될 수 있다.  
하지만 코드 주석에 따르면 타이밍 경로(`io.flush → mainPipe/prefetchPipe s2_miss → ftq ready`)를 위해  
flush 시에도 broadcast를 보내도록 설계되어 있다.

실제 broadcast를 보내더라도 수신 측(main pipe, prefetch pipe, WayLookup)은 자신의 `sx_valid`가 false이면 무시한다.

### 8.5 maybeRvcMap 계산

```scala
private val maybeRvcMap =
  VecInit(respDataReg.asTypeOf(Vec(MaxInstNumPerBlock, UInt((instBytes * 8).W))).map(_(1, 0) =/= 3.U)).asUInt
```

refill 데이터의 각 2바이트 슬롯에서 하위 2비트가 `0b11`이 아니면 RVC 명령이 아닐 수 있다는 힌트를 만든다.  
이 값은 MetaArray에도 쓰이고 broadcast를 통해 파이프라인에도 전달된다.

---

## 9. Victim Way 선택 (ICacheReplacer)

```scala
io.victim.req.valid        := acquireArb.io.out.fire
io.victim.req.bits.vSetIdx := acquireArb.io.out.bits.vSetIdx
private val waymask = UIntToOH(mshrInfo.way)
```

acquire가 fire하는 사이클에 Replacer에 victim way를 요청한다.  
Replacer의 응답은 같은 사이클에 combinational하게 나오고, MSHR가 `way` 레지스터에 래치한다.

`ICacheReplacer`는 두 개의 독립 replacer 인스턴스(`PortNumber = 2`)를 갖는다.  
vSetIdx의 LSB로 어느 replacer를 쓸지 결정한다.

```scala
io.victim.resp.way := Mux(
  io.victim.req.bits.vSetIdx(0),
  replacers(1).way(io.victim.req.bits.vSetIdx(idxBits - 1, 1)),
  replacers(0).way(io.victim.req.bits.vSetIdx(idxBits - 1, 1))
)
```

victim이 결정된 직후 다음 사이클에 Replacer 내부 touch 업데이트가 일어난다.  
(반대로 hit 시에는 main pipe가 `replacerTouch`를 보내 MRU 정보를 갱신한다.)

---

## 10. flush와 fencei 처리

| 신호 | 대상 MSHR | 동작 |
| --- | --- | --- |
| `fencei` | 모든 MSHR (fetch + prefetch) | `fencei = true`, `!issue`이면 `valid = false` |
| `flush` | prefetch MSHR만 | `flush = true`, `!issue`이면 `valid = false` |

fetch MSHR는 `io.flush`를 받지 않는다.

```scala
if (isFetch) {
  mshr.io.flush := false.B
} else {
  mshr.io.flush := io.flush
}
```

즉 BPU branch flush나 pipeline flush가 와도 fetch miss는 계속 처리된다.  
fetch는 실제 IFU 응답을 위한 것이므로, 취소 여부는 main pipe가 자체적으로 판단한다.

priorityFIFO는 flush/fencei 모두에 의해 flush된다.

```scala
priorityFIFO.io.flush.get := io.flush || io.fencei
```

---

## 11. WFI 지원

```scala
io.acquire.valid := valid && !issue && ... && !io.wfi.wfiReq
io.wfi.wfiSafe   := !(valid && issue)
```

WFI 요청이 오면 각 MSHR는 새 acquire를 발행하지 않는다.  
이미 L2에 보낸 요청(`issue = true`)이 모두 응답 완료되어야 `wfiSafe = true`가 된다.

MissUnit 전체의 `wfiSafe`는 모든 MSHR의 AND다.

```scala
io.wfi.wfiSafe := allMshr.map(_.io.wfi.wfiSafe).reduce(_ && _)
```

---

## 12. 신호 타이밍 정리

```text
Cycle 0  : acquire.fire → way 래치, issue = true, perf_latency = 0
Cycle 0~N: L2 beats 수신 (memGrant.fire, corruptReg/deniedReg 누적)
Cycle N  : lastFire → mshrInfo 래치 (1 사이클 선행)
Cycle N+1: lastFireNext
           → writeSramValid → metaWrite.valid, dataWrite.valid
           → respValid      → io.resp.valid (broadcast)
           → allMshr(id).invalid := true
```

SRAM write와 resp broadcast가 같은 사이클에 나가는 구조다.  
main pipe는 이 broadcast를 받아 아직 SRAM에 반영이 안 된 데이터도 즉시 사용한다.

---

## 13. 퍼포먼스 카운터 및 트레이스

### 13.1 XSPerfAccumulate

| 카운터 | 의미 |
| --- | --- |
| `enqFetchReq` | 실제로 MSHR에 등록된 fetch miss 수 |
| `enqPrefetchReq` | 실제로 MSHR에 등록된 prefetch miss 수 |
| `duplicateFetchReq` | 중복으로 차단된 fetch miss 수 |
| `duplicatePrefetchReq` | 중복으로 차단된 prefetch miss 수 (fetchReq와 겹친 경우 포함) |
| `prefetchHitFetchReq` | 같은 사이클에 fetch와 prefetch가 같은 block을 요청한 수 |

### 13.2 XSPerfHistogram

| 카운터 | 의미 |
| --- | --- |
| `fetchMshrEmptyCnt` | fetch MSHR 중 비어 있는 슬롯 수 분포 |
| `prefetchMshrEmptyCnt` | prefetch MSHR 중 비어 있는 슬롯 수 분포 |
| `responseLatency` (per MSHR) | acquire.fire ~ invalid 사이클 수 분포 |

### 13.3 ChiselDB 트레이스

| 테이블 | 기록 시점 | 주요 필드 |
| --- | --- | --- |
| `ICacheFetchMissTrace` | `fetchReq.fire` | blkPAddr, vSetIdx, mshr 번호, hitMshr |
| `ICachePrefetchMissTrace` | `prefetchReq.fire` | blkPAddr, vSetIdx, mshr 번호, hitMshr, hitFetch |
| `ICacheMissRespTrace` | `lastFireNext` | mshr 번호, victim way, latency, corrupt, denied, canceled |

`canceled = !mshrValid` — flush/fencei로 인해 SRAM write가 생략된 경우다.

---

## 14. 한 줄 결론

`ICacheMissUnit`의 핵심은 아래 세 가지로 요약된다.

1. **분리된 MSHR 풀**: fetch 4개 / prefetch 10개, 우선순위는 fetch 우선
2. **중복 차단**: 모든 MSHR를 combinational하게 동시 lookup해 duplicate를 같은 사이클에 차단
3. **단일 broadcast**: L2 Grant 완료 후 SRAM write와 파이프라인 응답을 같은 사이클에 발행해 재시도 없이 데이터 전달

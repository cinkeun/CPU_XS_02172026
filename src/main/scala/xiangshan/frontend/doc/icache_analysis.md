# XiangShan Instruction Cache (ICache) — Deep Dive Analysis

> **Reading Level Note:** This document is written to be understandable even if you have never seen a CPU before. Every concept is explained from scratch with simple analogies before the technical detail.

---

## Table of Contents

1. [What Is a Cache? (The Big Picture)](#1-what-is-a-cache-the-big-picture)
2. [ICache Top-Level Structure](#2-icache-top-level-structure)
3. [How Instructions Are Stored — Sets, Ways, and Cache Lines](#3-how-instructions-are-stored--sets-ways-and-cache-lines)
4. [Address Decoding — Finding the Right Shelf](#4-address-decoding--finding-the-right-shelf)
5. [Meta Array — The Label System](#5-meta-array--the-label-system)
6. [Data Array — The Storage Shelves](#6-data-array--the-storage-shelves)
7. [The Main Pipeline — Fetching Instructions](#7-the-main-pipeline--fetching-instructions)
8. [Cache Hits and Misses](#8-cache-hits-and-misses)
9. [Miss Unit and MSHRs — The Emergency Errand System](#9-miss-unit-and-mshrs--the-emergency-errand-system)
10. [Replacement Policy — Deciding What to Throw Away](#10-replacement-policy--deciding-what-to-throw-away)
11. [WayLookup Buffer — The Cheat Sheet](#11-waylookup-buffer--the-cheat-sheet)
12. [The Prefetcher — The Psychic Helper](#12-the-prefetcher--the-psychic-helper)
13. [ECC — Catching Corrupted Data](#13-ecc--catching-corrupted-data)
14. [Performance Counters and Monitoring](#14-performance-counters-and-monitoring)
15. [Key Parameters Reference](#15-key-parameters-reference)
16. [Data Flow Diagram — Everything Together](#16-data-flow-diagram--everything-together)

---

## 1. What Is a Cache? (The Big Picture)

### The Library Analogy

Imagine you are doing homework and need to read many books. Your school library is huge but far away — it takes a long time to go there and come back. To save time, you bring a small stack of your most-used books to your desk. Your desk is your **cache**.

- **Library** = Main memory (DRAM) — huge, slow
- **Desk** = Cache — small, fast
- **Books on your desk** = Cached instructions/data

A CPU (the brain of a computer) must read **instructions** (its "to-do list") from memory constantly. Every time it needs the next instruction, going all the way to main memory would be painfully slow. The **Instruction Cache (ICache)** is the CPU's personal desk: it keeps recently-used instructions close by so they can be read in just one or two clock cycles instead of hundreds.

### Why a Separate Instruction Cache?

Instructions and data (like numbers you add together) have different access patterns. Instructions are usually read **sequentially** — one after another — and often re-read when a loop repeats. Having a dedicated ICache optimized for this pattern is faster than sharing a single cache.

---

## 2. ICache Top-Level Structure

**Source file:** [icache/ICache.scala](../icache/ICache.scala), [icache/ICacheImp.scala](../icache/ICacheImp.scala)

Think of the ICache as a small organization. It has several departments that work together:

```
┌─────────────────────────────────────────────────────────────┐
│                         ICache                               │
│                                                              │
│  ┌───────────────────┐    ┌────────────────────────────┐    │
│  │  ICacheMainPipe   │    │    ICachePrefetchPipe       │    │
│  │  (Fetch highway) │    │    (Prediction highway)     │    │
│  └────────┬──────────┘    └──────────────┬─────────────┘    │
│           │                              │                   │
│  ┌────────▼──────────────────────────────▼──────────────┐   │
│  │              ICacheMissUnit                           │   │
│  │  (Emergency runner to L2 cache)                      │   │
│  │  4 Fetch MSHRs  +  10 Prefetch MSHRs                 │   │
│  └───────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌─────────────────────┐  ┌────────────────────────────┐    │
│  │   ICacheMetaArray   │  │     ICacheDataArray        │    │
│  │   (Tag labels)      │  │     (Instruction storage)  │    │
│  └─────────────────────┘  └────────────────────────────┘    │
│                                                              │
│  ┌─────────────────────┐  ┌────────────────────────────┐    │
│  │   ICacheReplacer    │  │     ICacheWayLookup        │    │
│  │   (Eviction policy) │  │     (Prefetch cheat sheet) │    │
│  └─────────────────────┘  └────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

| Module | Role | Analogy |
|---|---|---|
| `ICacheMainPipe` | Processes actual fetch requests from FTQ | The main checkout counter at a store |
| `ICachePrefetchPipe` | Speculatively fetches instructions before they're needed | A helpful assistant who pre-stocks shelves |
| `ICacheMissUnit` | Handles cache misses, communicates with L2 | Sends an errand runner to the library |
| `ICacheMetaArray` | Stores tag labels for each cached line | Index cards at the front of each shelf |
| `ICacheDataArray` | Stores the actual instruction bytes | The actual books on the shelves |
| `ICacheReplacer` | Decides which line to evict when full | The librarian who decides which book to remove |
| `ICacheWayLookup` | Queues prefetch results for the main pipe | A sticky-note system passed between helpers |

### TileLink — The Road to L2

The ICache communicates with the Level-2 (L2) cache using the **TileLink** protocol — a standardized "language" for cache-to-cache communication. When an instruction is not in the ICache, the `ICacheMissUnit` sends a `Get` message over TileLink to retrieve the missing cache line from L2.

---

## 3. How Instructions Are Stored — Sets, Ways, and Cache Lines

**Source file:** [icache/Parameters.scala](../icache/Parameters.scala)

### The Bookshelf Analogy Upgraded

Imagine a bookshelf with **256 rows** (called **sets**) and **4 columns** (called **ways**). Each cell in the shelf holds one **cache line** — a fixed-size block of 64 bytes (512 bits) of instructions.

```
         Way 0     Way 1     Way 2     Way 3
        ┌────────┬─────────┬────────┬────────┐
Set 0   │ Line A │ Line B  │ Line C │ Line D │
Set 1   │ Line E │  (empty)│ Line F │ Line G │
Set 2   │  (empty)│ Line H │ Line I │ Line J │
  ...   │  ...   │  ...    │  ...   │  ...   │
Set 255 │ Line K │ Line L  │ Line M │ Line N │
        └────────┴─────────┴────────┴────────┘
```

- **Set** = A row. The CPU uses bits from the memory address to pick which row to look in.
- **Way** = A column. Multiple items can be stored in the same row (they have different tags).
- **Cache Line** = One cell. Always 64 bytes. Instructions are packed into these 64-byte chunks.

### Key Numbers (Default Configuration)

| Parameter | Value | Meaning |
|---|---|---|
| `nSets` | 256 | 256 rows in the shelf |
| `nWays` | 4 | 4 columns (4-way set-associative) |
| `blockBytes` | 64 | Each cell = 64 bytes |
| Total capacity | 256 × 4 × 64 = 64 KB | Total instruction cache size |

### Two Ports

The ICache has **2 ports** (`PortNumber = 2`). This means it can service two fetch requests at the same time — like having two checkout lanes at a grocery store. The two ports access **different sets** (the sets are split: even sets go to port 0, odd sets go to port 1).

---

## 4. Address Decoding — Finding the Right Shelf

When the CPU wants instructions at address `X`, it must figure out which shelf row to look at and which label to check. This is done by splitting the address into parts.

### Virtual vs. Physical Addresses

The CPU programs use **virtual addresses** (like a room number in your school). The actual memory hardware uses **physical addresses** (the GPS coordinates of that room). A special unit called the **iTLB** (Instruction Translation Lookaside Buffer) translates between them. This translation happens in the prefetch pipe's Stage 0.

### Address Bit Breakdown

```
Virtual Address (64-bit):
 ┌──────────┬──────────────┬──────────────┬──────────────────────┐
 │  (upper) │  vSetIdx     │  bankIdx     │  rowOffset + byteOff │
 │          │  [13:6]      │  [5:3]       │  [2:0]               │
 │          │  8 bits      │  3 bits      │  3 bits              │
 │          │  → set (row) │  → data bank │  → byte within bank  │
 └──────────┴──────────────┴──────────────┴──────────────────────┘
                                ↑
                       blockOffset [5:0] = bankIdx[5:3] + byteOffset[2:0]

Physical Address (used for tag comparison):
 ┌──────────────────────────────────┬─────────────────┬──────────────┐
 │  pTag                            │  idx            │ blockOffset  │
 │  bits [PAddrBits-1 : pgUntagBits]│  [13:6]         │  [5:0]       │
 │  → the "name" of this line       │  → which set    │  byte offset │
 └──────────────────────────────────┴─────────────────┴──────────────┘
```

| Field | Bits | Width | Usage |
|---|---|---|---|
| `vSetIdx` | [13:6] | 8 bits | Selects 1 of 256 sets (rows) — **same** for virtual and physical |
| `bankIdx` | [5:3] | 3 bits | Selects 1 of 8 data banks within a cache line |
| `byteOffset` | [2:0] | 3 bits | Byte offset within an 8-byte bank row |
| `pTag` | [PAddrBits-1:14] | varies | Physical tag for hit/miss comparison |

**Interleaved bank index for Meta Array:**
```
MetaBankIdx  = vSetIdx[0]     → which interleaved meta bank (0 or 1)
MetaRowIdx   = vSetIdx[7:1]   → row within that bank (0..127)
```

- **`vSetIdx`** (8 bits) — Selects 1 of 256 sets. Since log₂(256) = 8, 8 bits are needed.
- **`bankIdx`** (3 bits, = `blockOffset[5:3]`) — Selects 1 of 8 data banks. Computed by `getBankIdx()` in [Helpers.scala](../icache/Helpers.scala).
- **`pTag`** — The "name badge" stored alongside each way. During lookup, the CPU compares this against the incoming physical address tag.

---

## 5. Meta Array — The Label System

**Source file:** [icache/ICacheMetaArray.scala](../icache/ICacheMetaArray.scala), [icache/ICacheMetaInterleavedBank.scala](../icache/ICacheMetaInterleavedBank.scala)

### What Is Metadata?

Each of the 256 × 4 = 1024 cache slots has a small label attached to it (metadata). This label answers: **"What is stored here?"** Without labels, you would not know if the bytes in slot (Set 3, Way 2) belong to address 0x80001000 or 0xFFFF0000.

### What the Metadata Contains

```
MetaEntryBundle (per set × per way):
┌─────────────────────────────────┬──────────────────┬──────────────────┐
│  pTag                           │  maybeRvcMap     │  ECC code        │
│  Physical tag bits              │  (MaxInstNum/Blk │  (parity, 1 bit) │
│  [PAddrBits-1 : pgUntagBits]    │   bits = 32b)    │                  │
└─────────────────────────────────┴──────────────────┴──────────────────┘
  stored per way                     valid bits stored as registers (not SRAM)
```

- **`pTag`** — The physical address tag. Used during lookup to confirm a hit.
- **`maybeRvcMap`** — A hint bitmap: each bit says "this 8-byte chunk *might* contain a compressed (RVC) instruction." Helps the IFU decode 2-byte vs 4-byte instructions correctly.
- **Valid bits** — Stored as flip-flop registers, **not** in SRAM. One bit per (set, way). Reset to 0 on `fence.i` or power-on.

### Physical SRAM Organization

```
Meta Array — Physical SRAM Layout
                                          MetaWaySplit = 2
                                         (4 ways → 2 SRAMs)
                ┌─────────────────────────────────────────────┐
                │  Interleaved Bank 0  (even sets: 0,2,4...) │
                │  ┌─────────────────┐ ┌─────────────────┐   │
                │  │  MetaSRAM[0][0] │ │  MetaSRAM[0][1] │   │
                │  │  128 sets       │ │  128 sets       │   │
                │  │  Way 0 & 1      │ │  Way 2 & 3      │   │
                │  └─────────────────┘ └─────────────────┘   │
                ├─────────────────────────────────────────────┤
                │  Interleaved Bank 1  (odd sets: 1,3,5...) │
                │  ┌─────────────────┐ ┌─────────────────┐   │
                │  │  MetaSRAM[1][0] │ │  MetaSRAM[1][1] │   │
                │  │  128 sets       │ │  128 sets       │   │
                │  │  Way 0 & 1      │ │  Way 2 & 3      │   │
                │  └─────────────────┘ └─────────────────┘   │
                └─────────────────────────────────────────────┘

Total: 4 physical SRAMs (2 interleaved banks × 2 way-splits)
Template: SplittedSRAMTemplate (set=128, way=4, waySplit=2, dataSplit=1)
Ports: singlePort = true (read and write cannot happen simultaneously)
Clock gate: enabled (withClockGate = true)
```

### Meta SRAM Access

```
bankSel = vAddr[6]             (1 bit,  = vSetIdx[0])      → interleaved bank 0 or 1
setIdx  = vAddr[13:7]          (7 bits, = vSetIdx[7:1])    → SRAM row 0–127 within bank
waySel  = ALL ways read        (read all 4 ways, no pre-select)
          → waymask is the OUTPUT: pTag comparison result
```

> **waySel이 없다.** Prefetch pipe는 meta SRAM에서 4 way를 전부 읽고 pTag를 비교한 뒤 waymask를 생성해 WayLookup에 저장한다.

**Why interleaving?** The ICache often needs two consecutive sets simultaneously (2-port design for cross-line fetches). Interleaving ensures consecutive sets are in different banks — no bank conflict.

```
vAddr[6]=0 (even sets)  → bank 0, row = vAddr[13:7]
vAddr[6]=1 (odd  sets)  → bank 1, row = vAddr[13:7]

Example:
  vSetIdx=0 (set 0)   → bank 0, row 0
  vSetIdx=1 (set 1)   → bank 1, row 0
  vSetIdx=2 (set 2)   → bank 0, row 1
  vSetIdx=3 (set 3)   → bank 1, row 1
```

### ECC on Metadata

Each meta entry is protected by **ECC** (Error Correcting Code) — explained in detail in [Section 13](#13-ecc--catching-corrupted-data). By default, **parity** is used: a single extra bit that catches 1-bit errors.

---

## 6. Data Array — The Storage Shelves

**Source file:** [icache/ICacheDataArray.scala](../icache/ICacheDataArray.scala), [icache/ICacheDataBank.scala](../icache/ICacheDataBank.scala)

### Physical SRAM Organization

Each 64-byte cache line is split across **8 banks** (`DataBanks = 8`), each holding 8 bytes (64 bits). Each bank further splits its 4 ways into **4 separate single-port SRAMs** — one per way.

```
Data Array — Physical SRAM Layout (8 banks × 4 ways = 32 SRAMs total)

         Way 0       Way 1       Way 2       Way 3
        ┌─────┐     ┌─────┐     ┌─────┐     ┌─────┐
Bank 0  │SRAM │     │SRAM │     │SRAM │     │SRAM │   byte  0– 7  (256 sets × 64-bit)
        ├─────┤     ├─────┤     ├─────┤     ├─────┤
Bank 1  │SRAM │     │SRAM │     │SRAM │     │SRAM │   byte  8–15
        ├─────┤     ├─────┤     ├─────┤     ├─────┤
Bank 2  │SRAM │     │SRAM │     │SRAM │     │SRAM │   byte 16–23
        ├─────┤     ├─────┤     ├─────┤     ├─────┤
Bank 3  │SRAM │     │SRAM │     │SRAM │     │SRAM │   byte 24–31
        ├─────┤     ├─────┤     ├─────┤     ├─────┤
Bank 4  │SRAM │     │SRAM │     │SRAM │     │SRAM │   byte 32–39
        ├─────┤     ├─────┤     ├─────┤     ├─────┤
Bank 5  │SRAM │     │SRAM │     │SRAM │     │SRAM │   byte 40–47
        ├─────┤     ├─────┤     ├─────┤     ├─────┤
Bank 6  │SRAM │     │SRAM │     │SRAM │     │SRAM │   byte 48–55
        ├─────┤     ├─────┤     ├─────┤     ├─────┤
Bank 7  │SRAM │     │SRAM │     │SRAM │     │SRAM │   byte 56–63
        └─────┘     └─────┘     └─────┘     └─────┘

Each SRAM: SRAMTemplate(set=256, way=1, singlePort=true)
Row width: 64 bits (data) + 1 bit (parity ECC) + 1 bit (padding) = 66 bits
```

### Data SRAM Access

```
setIdx  = vAddr[13:6]          (8 bits, log2(nSets=256))   → SRAM row 0–255
bankSel = vAddr[5:3]           (3 bits, log2(DataBanks=8)) → which bank 0–7
waySel  = WayLookup.waymask    (4 bits, one-hot)           → NOT from vAddr;
                                                              result of pTag comparison
                                                              done by prefetch pipe
```

> **waySel은 vAddr에서 오지 않는다.** Prefetch pipe가 meta SRAM을 읽고 pTag 비교를 수행한 결과(waymask)를 WayLookup에 저장해두면, main pipe S0에서 꺼내 SRAM way를 선택한다. (VIPT 구조)

### Cache Line → Bank Mapping

```
Cache Line (64 bytes)
Byte offset:  0    8   16   24   32   40   48   56
              ↓    ↓    ↓    ↓    ↓    ↓    ↓    ↓
            ┌────┬────┬────┬────┬────┬────┬────┬────┐
            │ B0 │ B1 │ B2 │ B3 │ B4 │ B5 │ B6 │ B7 │
            └────┴────┴────┴────┴────┴────┴────┴────┘
bankIdx:      0    1    2    3    4    5    6    7
            = blockOffset[5:3]
```

### What Data Banks Are (and Are Not)

Data Array의 8개 bank는 **하나의 cache line을 byte 위치별로 분할한 것**이다.

```
Bank 0 = 모든 set의 cache line에서 byte  0– 7 구간
Bank 1 = 모든 set의 cache line에서 byte  8–15 구간
...
Bank 7 = 모든 set의 cache line에서 byte 56–63 구간
```

따라서:
- 8개 bank는 **서로 다른 cache line을 독립적으로 저장하는 단위가 아니다.**
- "main pipe가 bank 0을 쓰고 있으니 bank 1–7을 prefetcher에 할당" 하는 것은 불가능하다.
  Bank 1–7은 bank 0과 **동일한 cache line의 다른 byte 구간**이기 때문이다.
- Bank 분할의 목적은 두 가지: **power saving** (필요한 byte 구간만 activate) 과 **cross-line access** 처리.

### Prefetcher의 실제 역할과 TLB port

Prefetch pipe는 bank를 활용하는 것이 아니라, FTQ가 예측한 **미래 fetch target (다음 cache line 주소)** 을 main pipe보다 먼저 처리하는 파이프라인이다.

```scala
// ICachePrefetchPipe.scala:47
val itlb: TlbRequestIO = new TlbRequestIO   // 단일 포트, Vec 아님

// line 154–155 (설계자 주석)
// we need only one Itlb port to get pAddr / itlbException,
// and we can simply use vAddr of first cacheline to send Itlb request.
```

**TLB port 1개로 충분한 이유:** prefetch pipe는 한 번에 request 하나를 순차 처리한다. Prefetch 병렬화의 병목은 TLB port 수나 SRAM bank 수가 아니라 **FTQ가 내려주는 prefetch target bandwidth (1 entry/cycle)** 이다.

```
Timeline:
  Cycle N:    Prefetch pipe → 주소 A (TLB + meta read → WayLookup write)
  Cycle N+1:  Prefetch pipe → 주소 B
  Cycle N+k:  Main pipe     → 주소 A (WayLookup에 결과 이미 있음 → TLB 없이 즉시 진행)
```

### Partial Bank Read (Power Saving)

The CPU often fetches instructions starting at an unaligned offset — e.g., 24 bytes into a 64-byte line. Only the **needed banks** are activated; the rest stay powered down.

**Bank selection helpers** ([Helpers.scala](../icache/Helpers.scala)):
- `getBankSel(blkOffset, blkEndOffset, crossLine)` — Returns a `Vec[Vec[Bool]]` bitmask: which banks of which line to read. Handles single-line and cross-line cases.
- `getLineSel(blkOffset)` — Returns per-bank line assignment: banks before `bankIdxLow` belong to the **second** line (wrap-around), others to the first.

### Cross-Line Access (doubleline)

When a fetch request spans the boundary of two 64-byte lines (e.g., fetch at offset 56 for 16 bytes → bytes 56–63 from line N, bytes 0–7 from line N+1), `doubleline = true`. Both lines are read simultaneously; `getBankSel` computes two separate bank masks.

### Read/Write Arbitration

```
Within each bank (ICacheDataBank):

  read.ready  :=  !write.valid  AND  way_sram.r.ready
                  ^^^^^^^^^^^
                  Refill (write) blocks reads — refill has priority

  write.ready :=  way_sram.w.ready   (independent of reads)
```

When refill is writing a line, any concurrent main-pipe read to the **same bank** is stalled for that cycle. Banks not involved in the refill are unaffected.

---

## 7. The Main Pipeline — Fetching Instructions

**Source file:** [icache/ICacheMainPipe.scala](../icache/ICacheMainPipe.scala)

This is the **critical path** — the pipeline that directly serves the CPU's fetch engine (FTQ = Fetch Target Queue). It has two stages.

### Stage 0 — Request and Dispatch

```
FTQ sends a fetch request
    → Decode address (vSetIdx, blockOffset)
    → Read WayLookup (pre-computed hit/miss info from prefetcher)
    → Send read request to DataArray (selected banks)
    → s0_fire = valid && wayLookup has data && DataArray ready && Stage 1 ready
```

The WayLookup provides a **predicted waymask** (which way to read from). If the prefetcher did its job, this is already computed, so Stage 0 sends directly to the right SRAM cells.

### Stage 1 — Data Return, Verification, and Response

```
    → Receive data from DataArray (or from MSHR if it was a prefetch hit)
    → Receive pTag from MetaArray (via WayLookup)
    → ECC check each bank
    → Exception prioritization:
        iTLB exception > PMP exception > L2 corrupt > ECC error
    → If miss: send MissReq to MissUnit
    → Send response to IFU with:
        - Up to 2 cache lines of instruction bytes
        - maybeRvcMap (for decoder hint)
        - Exception info
    → s1_fire = s1_valid && fetch is done && !respStall
```

**`s1_fetchFinish`** is true when: the data is available either from SRAM (cache hit) or from the MSHR (miss already being served and data returned).

### Flush Handling

The FTQ can **flush** in-flight requests when the branch predictor is corrected. Both S0 and S1 monitor flush signals:
- `s1_flush`: Clears Stage 1's valid bit.
- `s0_flush`: Clears Stage 0's valid bit.

Performance counters track how often each stage is flushed.

---

## 8. Cache Hits and Misses

### Hit — "It's Already on My Desk!"

A **hit** occurs when the requested cache line is already stored in the ICache.

**Hit Detection Sequence:**
1. From WayLookup, retrieve the stored `pTag` and `waymask` for the requested set.
2. Compare the request's physical address tag against the stored `pTag` in all 4 ways.
3. A **1-hot waymask** is generated: exactly one bit is `1` for the matching way.
4. `hit = orR(waymask)` — if any way matched, it's a hit.
5. Use the waymask to select the right way's data from the data array output.

Hit latency = **1 cycle** (read request sent in S0, data returned in S1).

### Miss — "I Don't Have That Book!"

A **miss** occurs when no way in the selected set has the requested line.

**Miss Sequence:**
1. `s1_shouldFetch = !hit || corruptRefetch` — pipeline detects miss.
2. Send `MissReqBundle` to `ICacheMissUnit`:
   - `blkPAddr` — the physical block address needed
   - `vSetIdx` — which set to fill into
3. MissUnit assigns a free **MSHR** entry.
4. MSHR sends a TileLink `Get` request to L2.
5. L2 responds with the cache line data (multiple beats).
6. MissUnit writes data back into the DataArray and MetaArray.
7. MissUnit sends a response to the main pipe.
8. Main pipe sends the instructions to IFU.

Miss latency = **many cycles** (dependent on L2 / DRAM latency).

---

## 9. Miss Unit and MSHRs — The Emergency Errand System

**Source file:** [icache/ICacheMissUnit.scala](../icache/ICacheMissUnit.scala), [icache/ICacheMshr.scala](../icache/ICacheMshr.scala)

### What Is an MSHR?

**MSHR** = Miss Status Holding Register. When the cache misses, instead of stopping everything, the CPU registers the miss in an MSHR and **keeps working on other things**. The MSHR is like a ticket given to an errand runner: "Please go get book X from the library. Here's ticket #3 so I can track it."

### MSHR Count and Priority

```
Total MSHRs: 14
  ├── 4 Fetch MSHRs       (for requests from ICacheMainPipe)
  └── 10 Prefetch MSHRs   (for requests from ICachePrefetchPipe)

Priority: Fetch > Prefetch
```

More fetch MSHRs have higher priority because they are on the **critical path** — the CPU is actively waiting for them. Prefetch MSHRs work in the background.

### MSHR State Machine

Each MSHR has these internal flags:

```
States:
  valid   — This MSHR is currently tracking a miss
  flush   — A flush was requested while this MSHR was active
  fencei  — A fence.i (instruction cache flush) was requested
  issue   — The TileLink Get request has been sent to L2

Lifecycle:
  (idle) → accept request → set valid=true
         → send TileLink Get → set issue=true
         → receive all beats from L2
         → write data to MetaArray and DataArray
         → send response to requester
         → clear valid (back to idle)
```

### Duplicate Detection

Before allocating a new MSHR, the Miss Unit checks if **any existing MSHR already covers the same address**. If yes, the new request is merged — no duplicate L2 requests. This is critical for efficiency: many fetches and prefetches might target nearby addresses.

Both the main pipe and prefetch pipe can query the MSHR status simultaneously:
- **FetchHit**: Main pipe's address matches an existing MSHR → no new MSHR needed, just wait.
- **PrefetchHit**: Prefetch pipe's address matches → no new MSHR needed.

### TileLink Refill

The L2 cache responds with data in multiple **beats** (chunks). For a 64-byte cache line, with a 256-bit (32-byte) data bus, there are `refillCycles = 2` beats. The MSHR collects all beats before writing to the data array.

During refill, **corrupt** and **denied** flags are tracked:
- **corrupt**: The data received has a checksum error.
- **denied**: L2 refused to serve (e.g., access to a protected region).

---

## 10. Replacement Policy — Deciding What to Throw Away

**Source file:** [icache/ICacheReplacer.scala](../icache/ICacheReplacer.scala)

### The Bookshelf Is Full — What Do We Remove?

When all 4 ways in a set are occupied and a new line must be brought in, one existing line must be **evicted** (thrown away). The replacement policy decides which one.

### Set-PLRU (Pseudo-LRU)

The default policy is **Set-PLRU** (set-associative Pseudo Least Recently Used).

**LRU idea:** Throw away the line that was **least recently used** — the book you haven't touched in the longest time.

**Pseudo-LRU** approximates true LRU with less hardware. Instead of a full timestamp per line, a binary tree of bits represents "which half was used more recently."

**Example with 4 ways and PLRU:**
```
       [bit A]
      /        \
  [bit B]    [bit C]
  /    \     /    \
Way0  Way1  Way2  Way3

- bit A = 0 → left side (Way0/Way1) was used more recently → victim is on right
- bit A = 1 → right side (Way2/Way3) was used more recently → victim is on left
- bit B/C narrow it down further
```

### Two Replacers for Two Ports

Since the ICache has 2 ports:
- **Replacer 0** manages **even sets** (Sets 0, 2, 4, ..., 254)
- **Replacer 1** manages **odd sets** (Sets 1, 3, 5, ..., 255)

Each manages 128 sets × 4 ways.

**Touch Operation (on hit):** When a way is accessed, its PLRU state is updated to mark it as "recently used."

**Victim Selection (on miss):** The PLRU tree is queried to find the "least recently used" way to evict.

---

## 11. WayLookup Buffer — The Cheat Sheet

**Source file:** [icache/ICacheWayLookup.scala](../icache/ICacheWayLookup.scala)

### The Problem WayLookup Solves

The main pipe needs to know **which way to read from** as early as possible (Stage 0), so it can send the right SRAM read address. But tag comparison (to determine the hit way) normally takes time.

The **prefetch pipe** runs ahead of the main pipe and pre-computes this information. WayLookup is the **message queue** between the prefetcher and the main pipe.

### Ring Queue Structure

```
WayLookup = Ring Queue with 32 entries (WayLookupSize = 32)

Each entry stores (for up to 2 ports):
  ┌──────────────────────────────────────────────────────────────┐
  │  waymask      [nWays bits, one-hot, per port]                │
  │  pTag         [tagBits per port] → physical tag              │
  │  metaCodes    [ECC bits]         → metadata ECC              │
  │  vSetIdx      [idxBits per port] → set index                 │
  │  itlbException                   → iTLB exception info       │
  │  gpAddr / isForVSnonLeafPTE      → guest page fault info     │
  └──────────────────────────────────────────────────────────────┘
```

### waymask 인코딩 — 0이 miss인가 way 0 hit인가?

waymask는 **one-hot 인코딩**이다.

```
  way 0 hit  →  waymask = 0b0001  (bit 0 set, non-zero)
  way 1 hit  →  waymask = 0b0010  (bit 1 set, non-zero)
  miss       →  waymask = 0b0000  (no bit set, zero)
```

`getWaymask`의 구현:
```scala
VecInit((pTags zip valids).map { case (pt, v) => v && pt === reqPTag }).asUInt
```
어느 way도 pTag가 일치하지 않으면 전 bit 0 → waymask = 0 = miss.
`waymask.orR`이 true면 hit, false면 miss. way 0 hit과 miss는 구별된다.

### Ordering 보장 — Prefetch와 Main Pipe의 매칭

Prefetch pipe는 **hit/miss 무관하게 항상 WayLookup에 write**한다.
miss인 경우 waymask=0으로 기록하고, 별도로 MSHR에 fetch 요청을 보낸다.
Main pipe는 WayLookup FIFO를 순서대로 read하므로, FTQ entry N에 대한 prefetch 결과는
항상 FTQ entry N에 대응하는 main pipe 요청과 자동으로 매칭된다.
별도의 tag/index matching 로직은 없으며, [ICacheMainPipe.scala:140](../icache/ICacheMainPipe.scala)의
`vSetIdx` assertion이 순서가 맞는지 런타임에 검증한다.

### Bypass Mode

WayLookup queue가 **비어있는** 상태에서 prefetch pipe가 write하는 바로 그 사이클에
main pipe가 read를 요청하면, 큐를 거치지 않고 **직접 bypass**된다 (대기 없음).

```
bypass 조건: empty && write.valid && !exceptionEntry.valid
```

### MSHR Update — 큐 내 entry 갱신

MissUnit이 L2 fetch를 완료하면 `MissRespBundle`을 broadcast한다.
WayLookup은 `io.update` 포트로 이를 수신해 **큐 안의 모든 entry에 대해** `updateMetaInfo`를 실행한다.

`updateMetaInfo`는 `vSetIdx`가 같은 entry에 대해 두 케이스를 처리한다:

| 조건 | 의미 | 처리 |
|------|------|------|
| `vSetSame && pTagSame` | 이 entry가 방금 fetch된 주소 | waymask 갱신 (miss → hit) |
| `vSetSame && waySame && !pTagSame` | 같은 set의 같은 way가 다른 주소로 교체됨 → 이 entry의 데이터가 evict됨 | **waymask := 0** (hit → miss) |

두 번째 케이스가 핵심이다. Prefetch가 "way 2에 hit"으로 WayLookup에 기록해두었더라도,
그 사이에 main pipe MSHR이 같은 set의 way 2를 다른 주소로 교체하면,
WayLookup 내 해당 entry의 waymask가 자동으로 0으로 초기화된다.
Main pipe가 이 entry를 읽으면 miss로 처리하여 re-fetch한다.

Main pipe가 entry를 읽기 전에 update가 완료된 경우:
- `updateStall = true` → main pipe S0가 1사이클 대기 → 올바른 waymask 읽음

Main pipe가 update 전에 이미 entry를 읽은 경우:
- S1에서 `s1_shouldFetch = true` → `s1_fetchFinish = false` → S1 스톨
- `fromMiss.valid`를 직접 모니터링 → MSHR 응답이 오면 해소

### Main Pipe의 MSHR 요청 — Fetch/Prefetch MSHR 중복 처리

Main pipe S1이 miss를 감지해 MissUnit에 fetch 요청을 보낼 때,
MissUnit은 **fetch MSHR과 prefetch MSHR 전체**를 동시에 탐색한다.

```scala
// ICacheMissUnit.scala
fetchHit := allMshr.map(_.io.lookUps(0).resp.hit).reduce(_ || _)

fetchDemux.io.in.valid := io.fetchReq.valid && !fetchHit  // fetchHit이면 새 MSHR 할당 안 함
io.fetchReq.ready      := fetchDemux.io.in.ready || fetchHit  // main pipe는 accepted 처리
```

| 상황 | 동작 |
|------|------|
| 어느 MSHR에도 없음 | 새 fetch MSHR 할당 → L2 요청 |
| prefetch MSHR에 이미 있음 | `fetchHit=true` → 새 MSHR 없음, broadcast 대기 |
| fetch MSHR에 이미 있음 | 동일 |

Prefetch MSHR에 이미 있는 경우 **promotion/transfer는 없다.**
Main pipe 요청은 drop되고, 기존 prefetch MSHR이 완료되면 `io.resp` broadcast가 발생한다.
Main pipe S1은 `fromMiss.valid`를 모니터링하다가 이 broadcast를 수신해 정상 진행한다.

단, prefetch MSHR은 flush 가능(`mshr.io.flush := io.flush`)하고
fetch MSHR은 flush 불가(`mshr.io.flush := false.B`)이다.
Prefetch MSHR이 flush될 경우 main pipe도 동일한 flush로 함께 비워지므로 문제없다.

### MissUnit Response — 단일 Broadcast 포트

MissUnit의 `io.resp`는 **포트가 하나**다. Fetch/prefetch MSHR 구분 없이,
어느 MSHR이든 L2 응답이 완료되면 그 결과를 단일 `ValidIO[MissRespBundle]`로 broadcast한다.

```
io.resp ──→ ICacheMainPipe    (fromMiss)
        ──→ ICachePrefetchPipe (fromMiss)
        ──→ ICacheWayLookup    (io.update)
```

수신 측은 각자 `vSetIdx + blkPAddr`을 확인해 자기 요청과 매칭되면 처리하고,
무관한 broadcast는 무시한다.

### Exception Tracking

WayLookup은 **첫 번째 exception만** 기록한다. Exception이 발생하면 반드시 redirect(flush)가
따라오기 때문에, 이후 entry는 어차피 버려진다. Exception entry가 존재하는 동안은
write를 차단하여 prefetch pipe를 stall시킨다(flush 대기).

---

## 12. The Prefetcher — The Psychic Helper

**Source file:** [icache/ICachePrefetchPipe.scala](../icache/ICachePrefetchPipe.scala)

### What Is Prefetching?

**Prefetching** means fetching instructions **before the CPU officially asks for them**. It's like a helpful assistant who, seeing you read page 50, quietly goes to get pages 51–60 from the library so they're on your desk when you get there.

Without prefetching: the main pipe has to wait for a miss to be resolved (slow).
With prefetching: the miss is already in progress or done by the time the main pipe needs the line (fast).

### Prefetch Sources

The ICache supports two types of prefetch:

1. **Hardware Prefetch** — Triggered by the **FTQ** (Fetch Target Queue). The branch predictor predicts future instruction addresses and sends them to the prefetch pipe ahead of time.

2. **Software Prefetch** — Triggered by a `prefetch.i` instruction in the program itself. A programmer (or compiler) can insert this instruction to say "warm up the cache for this address."

### Prefetch Pipe: 3 Stages

#### Stage 0 — Receive Request

```
Receive prefetch request (from FTQ or software)
    → Send iTLB request (translate virtual → physical address)
    → Send MetaArray read request (check if already in cache)
    → Record: vSetIdx, blockOffset, source (hw/sw)
```

#### Stage 1 — The FSM Heart

This stage runs a 5-state **Finite State Machine (FSM)** — a decision machine that changes states based on conditions.

```
States:
  Idle       → Ready for a new request
  ItlbResend → iTLB translation not done yet; keep resending request
  MetaResend → iTLB done, but MetaArray is busy; keep resending meta request
  EnqWay     → Everything ready; write result to WayLookup
  EnterS2    → Waiting for Stage 2 to accept the entry
```

**State Transitions:**

```
              ┌──────────────────────────────────────────────────────────┐
    ──→  ┌────┴─────┐                                                    │
         │   Idle   │ ◄──────────────────────────────────────────────────┤
         └────┬─────┘                                                    │
              │ s1_valid                                                  │
              ├─── !tlbFinish ────────────────────────────────────────┐  │
              │                                                        │  │
              ├─── tlbFinish && !toWayLookup.fire ──────────────────┐ │  │
              │                                                      │ │  │
              │ tlbFinish && toWayLookup.fire && !s2_ready          │ │  │
              ▼                                                      │ │  │
         ┌──────────────┐                                            │ │  │
         │  ItlbResend  │ ◄──────────────────┐ !tlbFinish           │ │  │
         └──────┬───────┘                    │                       │ │  │
                │ tlbFinish                  │                       │ │  │
                ├─── toMeta.ready ──────────────────────────────┐   │ │  │
                │                                                │   │ │  │
                │ !toMeta.ready                                  │   │ │  │
                ▼                                                ▼   ▼ │  │
         ┌──────────────┐  toMeta.ready               ┌──────────────┐  │
         │  MetaResend  │ ──────────────────────────→  │    EnqWay    │  │
         └──────────────┘                              └──────┬───────┘  │
              ▲  │ !toMeta.ready                              │           │
              └──┘                              toWayLookup.fire          │
                                               or isSoftPrefetch         │
                                                             │            │
                                          s2_ready           ▼            │
                                        ┌──────────→  ┌──────────┐       │
                                        │             │ EnterS2  │ ──────→┘
                                        │             └──────────┘  s2_ready
                                        │
                                   (stay Idle)
```

**Condition to enqueue to WayLookup:**
- iTLB translation complete (`tlbFinish`)
- No exception
- No concurrent SRAM write from Miss Unit (would cause conflict)
- If software prefetch: skip WayLookup enqueue (saves power — software prefetch just warms the MSHR, not WayLookup)

#### Stage 2 — Miss Detection and Request

```
Monitor MSHR update signals from MissUnit
    → For each port (0 and 1):
        - Is there a cache miss AND no exception AND not MMIO?
        - Is there NOT already an MSHR for this address?
        → YES: send MissReqBundle to MissUnit
    → Arbiter combines two-port requests (software prefetch gets priority)
```

**MMIO** = Memory-Mapped I/O. Devices like keyboards are accessed through special addresses. These should NOT be prefetched — fetching them would trigger device side-effects!

### Prefetch Enable Control

The prefetcher can be turned on/off via `csr.pfEnable`. When disabled, Stage 2 suppresses all prefetch miss requests.

### Power Consideration

Software prefetch requests deliberately skip WayLookup enqueue. They still warm the cache (fill MSHRs), but avoid unnecessary SRAM reads in the main pipe — saving power when the software prefetch is speculative.

---

## 13. ECC — Catching Corrupted Data

**Source file:** [icache/ICacheMetaArray.scala](../icache/ICacheMetaArray.scala), [icache/ICacheDataBank.scala](../icache/ICacheDataBank.scala)

### What Is ECC?

**ECC** = Error Correcting Code (or Error Checking Code). SRAM cells (the memory cells inside the cache) can sometimes **flip** a bit due to cosmic rays, electrical noise, or manufacturing defects. ECC detects (and sometimes corrects) these errors.

Think of it as a **checksum**: like how a phone number has a check digit to catch typos.

### Supported ECC Schemes

| Scheme | Setting | What It Does |
|---|---|---|
| `identity` | No ECC | No protection (dangerous) |
| `parity` | Default | 1 extra bit; detects 1-bit error, cannot correct |
| `sec` | Hamming SEC | Can **correct** 1-bit errors |
| `secded` | Hamming SEC-DED | Corrects 1-bit, detects 2-bit errors |

The default for both MetaArray and DataArray is **parity** — lightweight and fast.

### How Parity Works

Add all bits in the protected data together:
- If the count of `1` bits is **even** → parity bit = 0
- If the count of `1` bits is **odd** → parity bit = 1

When reading back, if the recomputed parity doesn't match the stored parity bit → **error detected**.

```
Original data: 1011 0110   (five 1s → odd → parity = 1)
Stored:        1011 0110  1   (data + parity bit)

Corrupted:     1011 0100  1   (bit 1 flipped)
Check:         four 1s → even → stored parity is 1 → MISMATCH → ERROR DETECTED!
```

### ECC in the Exception Chain

When an ECC error is detected in Stage 1 of the main pipe, it is reported as an exception. The exception priority is:

```
1. iTLB exception (highest priority)
2. PMP exception
3. L2 corrupt (data denial from L2)
4. ECC error (lowest priority)
```

### Test Support

The hardware has special force-fail registers (`ForceMetaEccFail`, `ForceDataEccFail`) that can intentionally corrupt ECC bits during testing. This verifies that the error-handling logic works correctly in silicon.

---

## 14. Performance Counters and Monitoring

**Source file:** [icache/ICacheMainPipe.scala](../icache/ICacheMainPipe.scala), [icache/ICacheMissUnit.scala](../icache/ICacheMissUnit.scala)

The ICache tracks many statistics to help engineers understand and optimize performance.

### Miss Rate Counters

| Counter | What It Counts |
|---|---|
| `icacheMissCnt` | Total cache misses (lines fetched from L2) |
| `icacheMissPenaltyCnt` | Total cycles spent waiting for L2 to respond |

### Prefetch Effectiveness

| Counter | What It Counts |
|---|---|
| `icachePrefetchHitCnt` | How often a prefetched line was actually used by the main pipe |
| `icachePrefetchMissCnt` | How often the prefetcher missed (prefetched wrong line) |
| `icacheHwPrefetchHitCnt` | Hardware prefetch hits specifically |
| `icacheHwPrefetchMissCnt` | Hardware prefetch misses |
| `icacheSwPrefetchHitCnt` | Software prefetch hits specifically |
| `icacheSwPrefetchMissCnt` | Software prefetch misses |

### Stall Analysis

| Counter | What It Counts |
|---|---|
| `icacheStallCnt` | Cycles where FTQ had a request but ICache couldn't accept it |
| `mainPipeStallCnt` | Cycles main pipe stalled waiting for MSHR response |
| `wayLookupStallCnt` | Cycles stalled waiting for WayLookup to have data |

### Flush Counters

| Counter | What It Counts |
|---|---|
| `icacheFlushS0Cnt` | Flushes that hit Stage 0 (early, less wasteful) |
| `icacheFlushS1Cnt` | Flushes that hit Stage 1 (later, more wasteful) |

A high `icacheFlushS1Cnt` suggests the branch predictor is frequently wrong — making the ICache do wasted work.

---

## 15. Key Parameters Reference

**Source file:** [icache/Parameters.scala](../icache/Parameters.scala)

### Base Configuration Parameters

```
┌──────────────────────┬────────────────┬───────────────────────────────────────────────┐
│ Parameter            │ Default Value  │ Description                                   │
├──────────────────────┼────────────────┼───────────────────────────────────────────────┤
│ nSets                │ 256            │ Number of cache sets (rows)                   │
│ nWays                │ 4              │ Number of ways (columns)                      │
│ blockBytes           │ 64             │ Cache line size in bytes                      │
│ rowBits              │ 64             │ Bits per data SRAM bank row                   │
│ DataBanks            │ 8              │ blockBits / rowBits = 512 / 64 = 8            │
│ PortNumber           │ 2              │ Concurrent access ports (for cross-line)      │
│ NumFetchMshr         │ 4              │ MSHRs for fetch requests                      │
│ NumPrefetchMshr      │ 10             │ MSHRs for prefetch requests                   │
│ WayLookupSize        │ 32             │ WayLookup ring queue depth                    │
│ Replacer             │ "setplru"      │ Replacement policy                            │
│ MetaEcc              │ "parity"       │ ECC scheme for MetaArray                      │
│ DataEcc              │ "parity"       │ ECC scheme for DataArray                      │
│ NumInterleavedBank   │ 2              │ Banks in MetaArray interleaving               │
│ MetaWaySplit         │ 2              │ Way groups per MetaArray physical SRAM        │
│ MaxInstNumPerBlock   │ 32             │ RVC map bits per 64B cache line               │
│ MaxInstNumPerBank    │ 2              │ RVC map bits per 8-byte bank                  │
│ EnableCorruptRefetch │ false          │ Re-fetch on ECC corruption (disabled timing)  │
└──────────────────────┴────────────────┴───────────────────────────────────────────────┘
```

### SRAM Access — Address Field Summary

```
┌─────────────────┬──────────────┬────────┬─────────────────────────────────────────────┐
│ SRAM            │ Field        │ vAddr  │ Note                                        │
├─────────────────┼──────────────┼────────┼─────────────────────────────────────────────┤
│ Data SRAM       │ setIdx       │ [13:6] │ 8 bits → row 0–255                          │
│                 │ bankSel      │ [5:3]  │ 3 bits → bank 0–7                           │
│                 │ waySel       │  —     │ WayLookup.waymask (NOT vAddr; pTag cmp out) │
├─────────────────┼──────────────┼────────┼─────────────────────────────────────────────┤
│ Meta SRAM       │ bankSel      │ [6]    │ 1 bit  → interleaved bank 0 or 1           │
│ (prefetch reads)│ setIdx       │ [13:7] │ 7 bits → row 0–127 within bank             │
│                 │ waySel       │  —     │ all ways read; waymask = pTag cmp output    │
└─────────────────┴──────────────┴────────┴─────────────────────────────────────────────┘
```

### Physical SRAM Count Summary

```
┌──────────────────┬──────────────────────────────────┬──────────────────────────────┐
│ Array            │ Structure                        │ Physical SRAMs               │
├──────────────────┼──────────────────────────────────┼──────────────────────────────┤
│ Data Array       │ 8 banks × 4 ways                 │ 32 SRAMs                     │
│                  │ Each: set=256, way=1, 66 bits/row│ (SRAMTemplate, singlePort)   │
├──────────────────┼──────────────────────────────────┼──────────────────────────────┤
│ Meta Array       │ 2 interleaved banks × 2 waySplit │ 4 SRAMs                      │
│                  │ Each: set=128, 2-ways, metaWidth  │ (SplittedSRAMTemplate)       │
├──────────────────┼──────────────────────────────────┼──────────────────────────────┤
│ Meta Valid bits  │ Registers (not SRAM)             │ 256 sets × 4 ways = 1024 FFs │
└──────────────────┴──────────────────────────────────┴──────────────────────────────┘

Total: 36 physical SRAMs  (32 data + 4 meta)
```

---

## 16. Data Flow Diagram — Everything Together

Here is the complete path of an instruction from the moment the CPU asks for it, to when it gets the bytes.

### Case A: Prefetch Hit (Best Case — 1 Cycle Latency)

```
Time →

Cycle N-k (ahead of time):
  FTQ → PrefetchPipe.S0 → [iTLB request] → [MetaArray read]
  PrefetchPipe.S1 → [ITLB result] → [waymask computed] → WayLookup.enqueue

Cycle N (main pipe starts):
  FTQ → MainPipe.S0 → WayLookup.read (gets waymask)
                    → DataArray.read (correct way, correct banks)

Cycle N+1 (data returns):
  MainPipe.S1 → DataArray returns data
             → ECC check ✓
             → Send instructions to IFU ✓  ← DONE in 1 pipeline cycle!
```

### Case B: Cache Miss (Worst Case — Many Cycles)

```
Cycle N:
  FTQ → MainPipe.S0 → WayLookup.read (shows miss)

Cycle N+1:
  MainPipe.S1 → Cache miss detected
             → MissReq sent to MissUnit
             → MSHR allocated
             → TileLink Get sent to L2

Cycles N+2 ... N+M:
  Waiting for L2 response...
  (L2 may itself miss and go to DRAM — even longer)

Cycle N+M:
  MissUnit receives all TileLink beats from L2
  → Writes data to DataArray and MetaArray
  → Sends response to MainPipe.S1

Cycle N+M+1:
  MainPipe.S1 → Data available
             → Send instructions to IFU ✓  ← DONE after M cycles of wait
```

### Case C: Prefetch Miss (Miss Already In-Flight)

```
Prefetch pipe sent MissReq to MissUnit already.
When MainPipe.S1 checks MSHRs, it finds an active MSHR for the address.
MainPipe.S1 waits for MSHR to complete.
→ Latency is less than Case B because the miss was started earlier.
```

### Summary Table

| Scenario | Where Data Comes From | Latency |
|---|---|---|
| Prefetch hit, SRAM hit | DataArray SRAM | 1 cycle (S0→S1) |
| MSHR hit (in-flight miss) | MSHR response | Variable (wait for L2) |
| Cold miss | L2 → MSHR → DataArray | Many cycles |

---

## Q&A: Meta Array vs WayLookup — TLB 결과 재사용 구조

### Q: Meta array가 따로 존재하는 건 TLB access 했던 걸 다시 사용하기 위해서 캐시하는 거지?

정확히는 두 구조의 역할이 다릅니다.

**Meta array** — 일반적인 set-associative cache의 표준 구성요소입니다. 각 cache line의 **physical tag (pTag)**, **maybeRvcMap**, **ECC code**를 저장합니다. TLB 결과를 재사용하기 위한 게 아니라, tag 비교를 위한 storage입니다.

**WayLookup buffer** — 이게 바로 "TLB access 했던 걸 다시 사용"하기 위한 구조입니다.

### 파이프라인 흐름 및 각 파이프의 array 접근

```
Prefetch pipe (pf0 → pf1 → pf2):
  pf0: iTLB 요청 + meta array SRAM 읽기
  pf1: iTLB 응답(pTag) + meta 응답 수신 → pTag 비교 → waymask 계산
       → WayLookup에 {waymask, pTag, metaCodes, ...} enqueue
  pf2: cache miss 여부 확인 → Miss Unit에 요청
  ※ data array 접근 없음

Main pipe (S0 → S1):
  S0: WayLookup에서 precomputed waymask를 읽어서 → data array read request 전송
  S1: data array response 수신 → IFU 전달
  ※ meta array 직접 읽기 없음 (WayLookup의 metaCodes 사용)
  ※ ECC 오류 발생 시에만 meta array write (flush)
```

| | Meta Array Read | Meta Array Write | Data Array Read |
|---|---|---|---|
| Prefetch pipe | O (pf0→pf1) | X | X |
| Main pipe | X (WayLookup으로 대체) | O (ECC 오류 시 flush) | O (S0→S1) |

Prefetch pipe가 **TLB + meta SRAM + tag 비교**를 미리 다 해놓고 WayLookup에 저장해두면, main pipe는 WayLookup만 읽어서 어느 way에서 data를 가져올지 바로 알 수 있습니다.

- **Meta array** = tag 비교용 storage (set-associative cache의 기본 구성)
- **WayLookup** = TLB + tag 비교 결과를 미리 계산해 저장하는 캐시

### WayLookup Buffer 상세

**물리적 구조:** 32-entry ring queue (FIFO). Prefetch pipe가 write, main pipe가 read.

**각 entry에 저장되는 정보:**

| 필드 | 크기 | 설명 |
|---|---|---|
| `vSetIdx[2]` | 8 bit × 2 | 요청한 virtual set index (포트 2개 지원) |
| `waymask[2]` | 4 bit × 2 | one-hot: 어느 way에 hit했는지 (0~3번 way 중) |
| `maybeRvcMap[2]` | 32 bit × 2 | meta array에서 읽은 RVC 압축 명령어 위치 bitmap |
| `metaCodes[2]` | ECC bits × 2 | meta array에서 읽은 ECC code |
| `pTag` | ~43 bit | iTLB가 반환한 physical tag |
| `itlbPbmt` | — | physical memory attribute (PBMT) |
| `itlbException` | — | iTLB에서 발생한 exception 정보 |
| `gpAddr` | — | guest physical address (Sv48x4 가상화용) |

**핵심 목적:** Main pipe S0에서 data array read를 시작하려면 "어느 way를 읽을지 (waymask)"를 그 사이클에 알아야 합니다. TLB + meta SRAM 접근 + tag 비교를 그 사이클에 즉석으로 하면 critical path가 너무 길어지므로, prefetch pipe가 미리 계산한 결과를 WayLookup에 저장해두고 main pipe가 꺼내 씁니다.

### Prefetch pipe와 Main pipe의 타이밍 관계

WayLookup은 **pf1에서 write**, main pipe **S0에서 read**이므로, 같은 FTQ entry 기준으로 prefetch pipe가 최소 **2 cycle** 앞서야 합니다.

```
cycle:    0      1      2      3
prefetch: pf0    pf1    pf2         ← pf1에서 WayLookup write
main:                   S0     S1   ← S0에서 WayLookup read
```

따라서 prefetch pipe는 main pipe보다 **여러 FTQ entry 앞선 것들을 미리 처리**해서 WayLookup에 쌓아두는 구조입니다. 32-entry ring queue는 이 가변적인 타이밍 차이를 흡수하기 위한 것으로, main pipe가 stall하면 entry가 쌓이고, prefetch pipe가 stall하면 main pipe가 WayLookup에서 기다립니다.

### ICache Access Latency

**Bypass 케이스 (WayLookup 비어 있을 때) — 최소 3 cycle:**

```
cycle 1: pf0       → meta array + iTLB request
cycle 2: pf1 = S0  → waymask 계산 + bypass → data array request (동시)
cycle 3: S1        → data array response → IFU
```

pf1과 S0가 같은 cycle에 겹치므로 최소 3 cycle.

**일반 케이스 (WayLookup에 entry 있을 때) — 최소 4 cycle:**

```
cycle 1: pf0  → meta array + iTLB request
cycle 2: pf1  → waymask 계산 → WayLookup write
cycle 3: S0   → WayLookup read → data array request
cycle 4: S1   → data array response → IFU
```

> **참고:** waymask는 "어느 way가 hit했는지"를 나타내는 one-hot 신호로, 일반적으로 wayselect라고도 불리는 개념입니다.

---

## ICache Prefetcher 개선 아이디어

현재 XiangShan ICache prefetch는 FTQ가 주는 next PC를 그대로 받아서 처리하는 단순한 구조입니다. Prefetch 강도 조절, pattern 감지, backend 상태 반영 등의 adaptive 요소가 전혀 없습니다. 아래는 성능 관점에서 고려할 수 있는 개선 방향입니다.

### 1. Backend Pressure-Aware Throttling

**현재 문제:** Backend가 heavy (ROB full, LSQ full 등)할 때도 prefetch pipe는 FTQ entry를 계속 소비합니다. 이 경우 fetch한 instruction이 어차피 decode/dispatch 단계에서 막히므로 prefetch가 의미 없는 energy를 소모합니다.

**개선 방향:** Backend로부터 backpressure signal (ROB occupancy threshold, dispatch stall 등)을 받아 prefetch 속도를 낮추거나 일시 정지. WayLookup이 가득 찬 상태를 유지하면서 불필요한 MSHR 소모를 줄일 수 있습니다.

**현재 상태:** `csrPfEnable`로 on/off만 가능. 중간 단계 없음.

---

### 2. iTLB Miss Bubble 감소

**현재 문제:** Prefetch pipe pf1에서 iTLB miss가 발생하면 WayLookup에 entry가 채워지지 않고, main pipe는 WayLookup이 빌 때까지 bubble을 삽입합니다. 이 상황은 코드에서 직접 topdown counter로 추적합니다:

```scala
// ICacheImp.scala line 230
io.toIfu.topdown.itlbMissBubble := prefetcher.io.perf.pendingItlbMiss && wayLookup.io.perf.empty
```

**개선 방향:**
- **iTLB prefetch:** prefetch pipe가 요청하는 virtual page를 미리 iTLB에 warm-up. 특히 함수 경계나 loop 진입 시 next page를 예측하여 선제 translation.
- **Page-crossing 예측:** 현재 fetch block이 page boundary에 근접할 때 next page translation을 미리 시작.

---

### 3. Prefetch Accuracy Feedback 부재

**현재 문제:** Prefetch한 cache line이 실제로 main pipe에서 사용되었는지 추적하는 feedback loop가 없습니다. 불필요한 prefetch가 cache pollution을 일으켜도 감지할 수 없습니다.

**개선 방향:** WayLookup entry가 main pipe에서 실제로 소비되었는지 (hit/miss 여부 포함) 추적하여 prefetch 정확도를 측정. 정확도가 낮으면 prefetch를 보수적으로 조정. MSHR을 fetch/prefetch 고정 분리 (현재 4/10)하는 대신 동적으로 재할당하는 방식도 고려 가능.

---

### 4. MSHR 고정 분리 문제

**현재 구조:** Fetch MSHR 4개, Prefetch MSHR 10개로 고정 분리 (`Parameters.scala`). Fetch miss가 몰릴 때 Prefetch MSHR을 빌려 쓸 수 없고, 반대로 prefetch miss가 없을 때 fetch MSHR 4개가 병목이 될 수 있습니다.

**개선 방향:** MSHR pool을 unified로 두고 fetch 요청에 우선순위를 부여하는 방식. Fetch miss는 항상 prefetch miss보다 먼저 L2 요청을 보냄으로써 fetch latency를 줄이면서도 total MSHR 활용도를 높일 수 있습니다.

---

### 5. WayLookup Bypass의 Timing Critical 경로

**현재 문제:** WayLookup이 비어 있을 때 pf1 write → S0 read bypass 경로가 존재하지만, 코드 주석에 `(maybe timing critical)`로 명시되어 있습니다:

```scala
// ICacheWayLookup.scala line 124
// if the entry is empty, but there is a valid write, we can bypass it to read port (maybe timing critical)
```

이 경로는 pf1의 combinational 결과가 같은 사이클 S0의 data array 주소로 직접 연결되므로, 타이밍 마진이 빠듯합니다.

**개선 방향:** Bypass 경로를 제거하고 대신 WayLookup을 항상 1 cycle 이상 ahead로 유지하도록 prefetch pipe를 더 앞서 실행. 혹은 timing critical 경로를 pipeline register로 끊고 bypass miss penalty를 1 cycle 허용하는 트레이드오프 고려.

---

### 6. L2 Early Hint로 MSHR Release 지연 숨기기

> **발견:** 코드 분석 중 확인된 개선 포인트

**현재 문제:** MSHR invalidation은 L2 grant last beat 도착 후 **1 cycle 지연** (`lastFireNext`)됩니다:

```scala
private val lastFireNext = RegNext(lastFire)          // 1 cycle delay
allMshr(i).io.invalid := lastFireNext && (idNext === i.U)
```

이 때문에 doubleline miss 기준 2개로 충분할 MSHR이 back-to-back miss를 처리하기 위해 4개 필요합니다 (active 2 + release pending 2). Release를 combinational하게 처리했다면 2개로 충분했을 것이나, timing closure를 위해 `RegNext`를 쓴 tradeoff입니다.

**개선 방향:** L2가 data를 보내기 2 cycle 전에 early hint signal을 보내면, prefetch pipe가 pf0를 그 시점에 시작할 수 있습니다:

```
현재:
  L2 grant last beat (N) → lastFireNext (N+1) → MSHR free → prefetch 시작

개선:
  L2 early hint    (N-2) → prefetch pipe pf0 시작
  L2 data          (N)   → SRAM write 완료 → 이미 pf1/pf2 진행 중
```

TileLink 스펙에는 이런 early hint 채널이 없으므로, L2(CoupledL2)와 ICache 사이에 custom signal을 추가하거나 **refill-triggered prefetch** 형태로 구현해야 합니다. Miss가 resolve될 때 인접 주소를 자동으로 prefetch하는 방식으로도 유사한 효과를 얻을 수 있으며, 현재 XiangShan에는 이 역시 구현되어 있지 않습니다.

---

### 요약

| 개선 항목 | 현재 한계 | 기대 효과 |
|---|---|---|
| Backend throttling | on/off만 가능 | 불필요한 energy/MSHR 소모 감소 |
| iTLB miss bubble | 감지만 함, 완화 수단 없음 | fetch bubble 감소 |
| Prefetch accuracy feedback | 없음 | cache pollution 방지, MSHR 효율화 |
| MSHR 동적 할당 | 고정 4/10 분리 | fetch latency 감소, 활용도 향상 |
| Bypass timing | timing critical 경로 존재 | timing closure 개선 |
| L2 early hint | 없음 (TileLink 비지원) | MSHR release 지연 숨기기, miss latency 감소 |

---

## Appendix: Source File Map

| File | Role |
|---|---|
| [icache/ICache.scala](../icache/ICache.scala) | Top-level LazyModule, TileLink node |
| [icache/ICacheImp.scala](../icache/ICacheImp.scala) | Module instantiation, wiring |
| [icache/ICacheMainPipe.scala](../icache/ICacheMainPipe.scala) | Fetch pipeline (S0, S1) |
| [icache/ICachePrefetchPipe.scala](../icache/ICachePrefetchPipe.scala) | Prefetch pipeline (S0, S1, S2) |
| [icache/ICacheMissUnit.scala](../icache/ICacheMissUnit.scala) | MSHR orchestration, TileLink |
| [icache/ICacheMshr.scala](../icache/ICacheMshr.scala) | Single MSHR state machine |
| [icache/ICacheMetaArray.scala](../icache/ICacheMetaArray.scala) | Tag + RVC map SRAMs |
| [icache/ICacheDataArray.scala](../icache/ICacheDataArray.scala) | Instruction data SRAMs |
| [icache/ICacheDataBank.scala](../icache/ICacheDataBank.scala) | Single data bank SRAM |
| [icache/ICacheReplacer.scala](../icache/ICacheReplacer.scala) | Set-PLRU replacement logic |
| [icache/ICacheWayLookup.scala](../icache/ICacheWayLookup.scala) | Ring queue between prefetch and main |
| [icache/Parameters.scala](../icache/Parameters.scala) | All configuration parameters |
| [icache/Bundles.scala](../icache/Bundles.scala) | Signal bundle definitions |
| [icache/Helpers.scala](../icache/Helpers.scala) | Utility functions |

---

*Generated: 2026-03-17 | Branch: kunminghu-v3 | XiangShan CPU Project*

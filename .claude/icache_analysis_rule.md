# ICache Analysis Rule

This rule defines the required structure and content for `src/main/scala/xiangshan/frontend/doc/icache_analysis.md`.

## 1. Scope

The analysis must be code-based. Use the current RTL and Chisel source under:

- `src/main/scala/xiangshan/frontend/icache/`
- `src/main/scala/xiangshan/frontend/`
- related interface bundles in `src/main/scala/xiangshan/frontend/Bundles.scala`
- related MMU/PMP interfaces only when they are directly connected to ICache

Do not infer undocumented blocks from external articles or memory. If a block, interface, or behavior is described, it must be backed by source code.

## 2. Required Output Order

The generated ICache analysis document must use this order:

1. Block diagram
2. Top-level interface table
3. TLB-hit/cache-hit operation and prefetch operation, each with a Mermaid sequence diagram
4. MetaArray and DataArray SRAM layout, with PNG image references and draw.io source links
5. DataArray source and current-cycle arbitration
6. Mermaid sequence diagrams for all TLB hit/miss and cache hit/miss cases
7. Link to `icache_missunit_analysis.md`

## 3. Block Diagram Section

The block diagram section must include:

- PNG image reference: `./icache_block_diagram.png`
- draw.io source link: `./icache_block_diagram.drawio`
- a short explanation of the main dataflow:
  - FTQ prefetch request to `ICachePrefetchPipe`
  - iTLB and MetaArray lookup
  - WayLookup enqueue
  - FTQ fetch request to `ICacheMainPipe`
  - WayLookup dequeue and DataArray read
  - MissUnit refill path through TileLink

If a PNG export tool is not available in the local environment, keep the image reference in the document and clearly state in the final response that PNG export could not be generated locally.

## 4. Interface Section

The interface table must document the current ICache top-level interface from `ICacheImp.ICacheIO`.

The table must include at least:

- `hartId`
- `fromFtq`
- `softPrefetchReq`
- `toIfu`
- `fromIfu`
- `pmp`
- `itlb`
- `itlbFlushPipe`
- `error`
- `csrPfEnable`
- `fencei`
- `flush`
- `wfi`

Each row must include direction, type/protocol, and a short description.

## 5. TLB Hit and Cache Hit Section

The TLB-hit/cache-hit section must describe both:

- the standalone prefetch operation through `ICachePrefetchPipe`
- the full TLB-hit/cache-hit fast path from prefetch lookup to MainPipe response

The generated analysis document should place the prefetch operation sequence diagram inside document section 3, before or immediately near the TLB-hit/cache-hit fast-path diagram.

The standalone prefetch operation sequence diagram must show:

- FTQ or soft prefetch request arbitration into `ICachePrefetchPipe`
- S0 iTLB request issue
- S0 MetaArray read issue
- S1 iTLB response handling
- S1 MetaArray response handling
- tag compare, valid check, and MSHR refill update consideration
- PMP/cacheability check
- WayLookup write on prefetch completion
- prefetch miss request to `ICacheMissUnit` only when miss, prefetch is enabled, and the request is cacheable/non-exception
- no direct DataArray access from `ICachePrefetchPipe`

The TLB-hit/cache-hit fast-path description must cover all relevant submodules:

- `ICachePrefetchPipe`
- iTLB
- `ICacheMetaArray`
- `ICacheWayLookup`
- `ICacheMainPipe`
- `ICacheDataArray`
- PMP checker
- `ICacheReplacer`
- IFU

The Mermaid sequence diagram must show:

- prefetch request acceptance
- iTLB request and response
- MetaArray read and response
- WayLookup write
- fetch request acceptance
- WayLookup read
- DataArray read and response
- PMP check
- IFU response
- replacer touch
- no MissUnit request on cache hit

## 6. SRAM Layout Section

The SRAM layout section must include:

- PNG image reference: `./icache_meta_array_sram_layout.png`
- draw.io source link: `./icache_meta_array_sram_layout.drawio`
- PNG image reference: `./icache_sram_layout.png`
- draw.io source link: `./icache_sram_layout.drawio`

It must explain why MetaArray and DataArray use different interleaving organizations.

Required points:

- MetaArray uses `NumInterleavedBank = 2` because it must support concurrent lookup of two adjacent cachelines for one fetch bundle (`PortNumber = 2`).
- MetaArray stores tags, valid state, RVC hints, and meta ECC; it is read by PrefetchPipe and written/flushed by MissUnit/MainPipe.
- DataArray is organized by byte banks (`DataBanks = blockBits / rowBits = 8`) and ways; one 64B line is split across eight 64-bit data banks.
- DataArray does not use the MetaArray set-interleaving scheme; it uses data-bank selection based on the fetch byte range and cross-line access.
- MetaArray interleaving reduces adjacent-line tag lookup conflicts, while DataArray banking provides sub-line data extraction bandwidth.

## 7. DataArray Source and Arbitration Section

The analysis document must include a dedicated section after the SRAM layout section titled:

```markdown
## 5. DataArray Source and Current-Cycle Arbitration
```

This section must identify every architecturally relevant DataArray access source from the current RTL and explain how each source reaches, or does not directly reach, `ICacheDataArray`.

Required source categories:

- Demand fetch read:
  - Source: `ICacheMainPipe`
  - Connection: `dataArray.io.read <> mainPipe.io.dataRead`
  - Meaning: reads instruction data for the current FTQ fetch request after WayLookup supplies way information.
- Refill write:
  - Source: `ICacheMissUnit`
  - Connection: normally `dataArray.io.write <> missUnit.io.dataWrite`
  - Meaning: writes a refilled cacheline from TileLink grant data into the selected set and way.
- Control/ECC injection write:
  - Source: `ICacheCtrlUnit` when `EnableCtrlUnit` and `ctrlUnit.io.injecting`
  - Connection: `dataArray.io.write <> ctrlUnit.io.dataWrite`
  - Meaning: overrides MissUnit write access during fault injection.
- Prefetch:
  - Source: `ICachePrefetchPipe`
  - Meaning: prefetch does not directly read or write `ICacheDataArray`; it reads `ICacheMetaArray`, writes `ICacheWayLookup`, and sends prefetch miss requests to `ICacheMissUnit`. A successful prefetch fill reaches DataArray only through MissUnit refill write.

The section must include a source table with at least:

- source name
- module
- DataArray port used
- direct or indirect access
- priority or blocking behavior

The section must describe the current-cycle arbitration algorithm exactly as implemented:

- Top-level write-port owner selection in `ICacheImp`:
  - If `EnableCtrlUnit` and `ctrlUnit.io.injecting`, CtrlUnit owns `dataArray.io.write` and `missUnit.io.dataWrite.req.ready := false.B`.
  - Otherwise, MissUnit owns `dataArray.io.write`; if CtrlUnit exists, `ctrlUnit.io.dataWrite.req.ready := false.B`.
- Inside `ICacheDataArray`:
  - `read` fans out to the selected `ICacheDataBank` instances based on `getBankSel` and `getLineSel`.
  - `write` fans out to all `DataBanks` for the refill cacheline.
  - `io.read.req.ready := banks.map(_.io.read.req.ready).reduce(_ || _)`.
  - `io.write.req.ready := banks.map(_.io.write.req.ready).reduce(_ && _)`.
- Inside each `ICacheDataBank`:
  - each way is a `SRAMTemplate(..., singlePort = true)`.
  - `read.req.ready := !write.req.valid && all way read ports ready`.
  - write valid directly drives the selected way SRAM write port.
  - Therefore, same-bank read loses to write in the same cycle.

The section must explicitly state whether this arbitration is FSM-based or simple arbitration:

- Current DataArray access arbitration is simple combinational ready/valid priority logic, not a state machine.
- The only state involved is the SRAM read request register in `ICacheDataBank` and optional top-level CtrlUnit injection state outside DataArray.

Because the DataArray arbitration is not a state machine, the analysis must include the main arbitration algorithm instead of a state transition diagram. Use pseudocode similar to:

```text
if EnableCtrlUnit && ctrlUnit.injecting:
    DataArray.write <- CtrlUnit.dataWrite
    MissUnit.dataWrite.ready <- false
else:
    DataArray.write <- MissUnit.dataWrite
    CtrlUnit.dataWrite.ready <- false

for each DataBank:
    if write.valid:
        read.ready <- false
        perform write to selected way
    else if read.valid && selected_by_bankSel:
        perform read from selected way
```

The section must explain the implication for demand, refill, and prefetch:

- Demand read can proceed only when the selected DataBank is not accepting a write.
- Refill write has priority over demand read within each `ICacheDataBank`.
- Prefetch has no direct DataArray read port; prefetch only consumes DataArray bandwidth later if it becomes a MissUnit refill write.
- CtrlUnit injection, when active, blocks MissUnit DataArray refill writes at the top-level mux.

## 8. All Cases Section

The all-cases section must cover:

- TLB hit + cache hit
- TLB hit + cache miss
- TLB miss + cache hit
- TLB miss + cache miss
- TLB exception
- MMIO/PMP/uncacheable path as observed by PrefetchPipe/MainPipe

Use Mermaid sequence diagrams. Diagrams may be grouped, but the behavior of each case must be explicit.

## 9. MissUnit Link Section

The final section must link to:

- `./icache_missunit_analysis.md`

The text must state that detailed MSHR allocation, duplicate request filtering, TileLink acquire/grant handling, and refill writeback are covered in that document.

## 10. Style

- Write the analysis in English.
- Prefer concise technical prose.
- Use exact module names from the RTL.
- Use Markdown tables for interfaces and memory layout summaries.
- Use Mermaid `sequenceDiagram` blocks for sequence diagrams.
- Keep draw.io links relative to the document location.

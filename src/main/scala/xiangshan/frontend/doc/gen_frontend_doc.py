#!/usr/bin/env python3
"""
Generate XiangShan Frontend Combined Documentation (DOCX)
Combines all frontend_*.md files, converts Mermaid diagrams to text, all in English.
"""

from docx import Document
from docx.shared import Pt, RGBColor, Inches, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import re

# ─── Helpers ──────────────────────────────────────────────────────────────────

def add_heading(doc, text, level=1):
    doc.add_heading(text, level=level)

def add_para(doc, text, bold=False, italic=False, style=None):
    if style:
        p = doc.add_paragraph(style=style)
    else:
        p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = bold
    run.italic = italic
    return p

def add_code_block(doc, text):
    """Add a monospace code block paragraph."""
    for line in text.split('\n'):
        p = doc.add_paragraph(style='No Spacing')
        run = p.add_run(line if line else ' ')
        run.font.name = 'Courier New'
        run.font.size = Pt(8)

def add_table_from_rows(doc, rows):
    """rows: list of lists of strings. First row = header."""
    if not rows:
        return
    ncols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=ncols)
    table.style = 'Table Grid'
    for i, row in enumerate(rows):
        for j, cell_text in enumerate(row):
            cell = table.cell(i, j)
            cell.text = cell_text
            if i == 0:
                for run in cell.paragraphs[0].runs:
                    run.bold = True

def parse_md_table(lines):
    """Parse markdown table lines → list of row-lists."""
    rows = []
    for line in lines:
        line = line.strip()
        if not line or set(line.replace('|','').replace('-','').replace(':','').strip()) == set():
            continue
        if re.match(r'^\|[-:| ]+\|$', line):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        rows.append(cells)
    return rows


# ─── Pre-converted ASCII diagrams ─────────────────────────────────────────────

BLOCK_DIAGRAM = r"""
XiangShan Frontend Pipeline — Block Diagram
============================================

         CSR/sfence/tlbCsr/csrCtrl
                   |
                   v
  +---------------------------------+
  |       Predictor (BPU)           |
  |  S0: PC mux, PHR/GHR gen        |
  |    |                            |
  |  S1: UBTB+ABTB+UTAGE+MicroRAS  |  <-- 1st prediction (1 cycle)
  |    |  s1_fire --> prediction.v  |
  |  S2: MBTB+TAGE+SC              |  <-- (internal only, no FTQ send)
  |    |                            |
  |  S3: MBTB(latched)+ITTAGE+RAS  |  <-- s3_override? --> FTQ update
  +--------+-----------------------+
           |  bpu_to_ftq (Decoupled)         ^ toBpu.redirect/update (Valid)
           v                                 |
  +---------------------------------+        |
  |             Ftq                 |--------+
  |  bpuPtr  <-- BPU enqueue        |
  |  ifuPtr  --> IFU dequeue        |------> IFU (toIfu.req)
  |  commPtr <-- ROB commit         |------> ICache (toICache.req)
  |                                 |------> Prefetcher (toPrefetch)
  +--------+------------------------+
           |  toIfu.req (Decoupled)   flushFromBpu (BpuFlushInfo)
           |
           v
  +---------------------------------+
  |          NewIFU                 |       +---------------------+
  |  F0: fetch req to ICache        |       |       ICache        |
  |  F1: PC calc (CatPC adder)      |<------| MainPipe (S0/S1/S2) |
  |  F2: ICache resp, Predecode     |       | MissHandler         |
  |  F3: RVC expand, PredChecker    |       | Prefetcher          |
  +--------+------------------------+       +---------+-----------+
           |  toIbuffer (Decoupled)                   |
           |  pdWb (Valid) --> FTQ                    v
           v                               +------------------+
  +---------------------------------+      |   iTLB / PMP     |
  |           IBuffer               |      |  TLB PortNum+1   |
  |  IBufNBank=6 banked FIFO        |      |  PMP+Checker     |
  |  48 entries, interleaved        |      +------------------+
  |  Output Reg (1 stage)           |
  +--------+------------------------+
           |  cfVec (DecoupledIO Vec, DecodeWidth=6)
           v
  +---------------------------------+
  |     Backend / Decode            |<-- rob_commits
  |                                 |--> toFtq.redirect (mispred)
  +---------------------------------+

Key Parameters:
  FtqSize=64, IBuffer.Size=48, IBufNBank=6, FetchBlockInstNum=16
  DecodeWidth=6, PhrHistoryLength=(computed), GhrHistoryLength=(SC max)
  FetchBlockSize=64B, FetchBlockAlignSize=32B
"""

SEQ_NORMAL = r"""
Sequence Diagram A: Normal Fetch (ICache Hit)
=============================================
BPU             FTQ             IFU             ICache          IBuffer         Backend
 |               |               |               |               |               |
 | [C0] S0: PC mux, history gen  |               |               |               |
 |               |               |               |               |               |
 | [C1] S1: UBTB+ABTB+UTAGE+MicroRAS lookup     |               |               |
 |--prediction-->|               |               |               |               |
 |  startPc,     |               |               |               |               |
 |  ftqIdx,      |               |               |               |               |
 |  s3Override=0 |               |               |               |               |
 |               |--toIfu.req--->|               |               |               |
 |               |  startAddr,   |               |               |               |
 |               |  nextline,    |               |               |               |
 |               |  ftqIdx       |               |               |               |
 |               |--toICache.req------------>|               |               |
 |               |               |               |               |               |
 |               |         [C2] F1: PC calc (CatPC adder), f1_valid=1           |
 |               |               |               |               |               |
 |               |         [C3] F2: ICache resp received          |               |
 |               |               |<--fetch.resp.valid------------|               |
 |               |               |  data, exception, paddr       |               |
 |               |         [C3] F2: predecode, exception, instr_range            |
 |               |               |               |               |               |
 |               |         [C4] F3: RVC expand, PredChecker, f3_valid=1         |
 |               |               |--toIbuffer.valid------------->|               |
 |               |               |  instrs, pd, pc, ftqPtr       |               |
 |               |<--pdWb.valid--|               |               |               |
 |               |               |               |         [C5] output          |
 |               |               |               |               |--cfVec(0..5)->|
"""

SEQ_B1 = r"""
Sequence Diagram B-1: S3 Override (ifuPtr > s3FtqPtr — IFU already ahead)
===========================================================================
  Context: S1 enqueues idx=X → IFU processes idx=X,X+1 → S3 finds tgt_C != tgt_A

BPU             FTQ             IFU
 |               |               |
 | [C0] S1 fire: PC_A → idx=X enq, s3Override=0, tgt=tgt_A
 |--prediction-->|               |
 |               | entryQueue(X).startPc:=PC_A, bpuPtr:=X+1
 |               |--toIfu.req(idx=X, startAddr=PC_A)-->|
 |               |               |               ifuPtr=X
 |               |               |
 | [C1] S1 fire: PC_A+blk → idx=X+1, s3Override=0
 |--prediction-->|               |
 |               | entryQueue(X+1):=PC_A+blk, bpuPtr:=X+2
 |               |--toIfu.req(idx=X+1)--------->|
 |               |               |               ifuPtr=X+1
 |               |               |
 | [C2] S3 fire: s3_prediction(PC_A)=tgt_C != s1_prediction=tgt_A → s3_override=1
 |               | s2_flush=1 (BPU internal S1+S2 in-flight kill), s0_startPc:=tgt_C
 |--prediction(s3Override=1, s3FtqPtr=X, tgt=tgt_C)-->|
 |               | bpuS3Redirect=1                |
 |               | entryQueue(X).tgt := tgt_C     |
 |               | bpuPtr := X+1 (discard X+1)    |
 |               | ifuPtr=X+1 >= s3FtqPtr=X → ifuPtr:=X (rollback)
 |               |--flushFromBpu.s3(valid=1, bits=X)-->|
 |               |               | shouldFlushByStage3(idx>=X) → all in-flight invalidated
 |               |               |
 | [C3] FTQ re-issues IFU request with corrected target
 |               | bpuPtr=X+1, ifuPtr=X → bypass-2 applied
 |               |--toIfu.req(idx=X, nextStartVAddr=tgt_C)-->|
"""

SEQ_B2 = r"""
Sequence Diagram B-2: Early S3 Override (ifuPtr <= s3FtqPtr — IFU still behind)
=================================================================================
  Context: FTQ back-pressure stalls IFU → ifuPtr < s3FtqPtr

BPU             FTQ             IFU
 |               |               |
 | [C0] S1 fire: PC_A → idx=X enq, IFU stall (ifuPtr=X-1)
 |--prediction-->|               |
 |               | entryQueue(X):=PC_A, bpuPtr:=X+1
 |               |               IFU stall: ifuPtr=X-1
 |               |               |
 | [C1] S2: internal BPU processing
 |               |               |
 | [C2] S3 fire: s3_prediction(PC_A)=tgt_C != s1_prediction → s3_override=1
 |--prediction(s3Override=1, s3FtqPtr=X, tgt=tgt_C)-->|
 |               | bpuS3Redirect=1                |
 |               | entryQueue(X).tgt := tgt_C     |
 |               | bpuPtr := X+1                  |
 |               | ifuPtr=X-1 < s3FtqPtr=X → NO rollback
 |               |--flushFromBpu.s3(valid=1, bits=X)-->|
 |               |               | shouldFlushByStage3(idx=X-1):
 |               |               |   !isAfter(X, X-1) = false → no flush
 |               |               |   in-flight fetch idx=X-1 preserved
 |               |               |
 | [C3] IFU requests idx=X with updated tgt_C
 |               |--toIfu.req(idx=X, nextStartVAddr from SRAM=tgt_C)-->|
"""

SEQ_B3 = r"""
Sequence Diagram B-3: Back-pressure S3 Override (prediction.ready=0)
=====================================================================
  Context: FTQ full → new S1 enqueue blocked, but s3_override executes independently

BPU             FTQ             IFU
 |               |               |
 | [C0] FTQ full: prediction.ready=0
 |--prediction(s3Override=0)-->| (new S1 attempt)
 |               |<--ready=0   | (FTQ full: bpuPtr - deqPtr >= FtqSize)
 |               |               s1_fire=0 (stall)
 |               |               Note: S3 has PC_A already latched
 |               |               |
 | [C1] S3 override fires (independent of prediction.ready)
 | S3: s3_prediction(PC_A)=tgt_C != s1_prediction → s3_override=1
 |--prediction(s3Override=1, s3FtqPtr=X, tgt=tgt_C)-->|
 |               | bpuS3Redirect = prediction.valid && s3Override = 1 (ready-independent)
 |               | entryQueue(X).tgt := tgt_C
 |               | bpuPtr := X+1
 |               | ifuPtr >= X → ifuPtr:=X (rollback)
 |               |--flushFromBpu.s3(valid=1, bits=X)-->|
 |               |               | IFU flush
 |               |               |
 | [C2] FTQ slot freed → prediction.ready=1
 |               |<--ready=1   |
 |--prediction(s3Override=0, startPc=tgt_C)-->|
 |               | entryQueue(X): new S1 enq
 |               |--toIfu.req(idx=X, startAddr=PC_A, nextStartVAddr=tgt_C)-->|
"""

SEQ_C = r"""
Sequence Diagram C: Backend Redirect (Misprediction Flush)
===========================================================
  Context: ROB detects misprediction → full Frontend flush → restart from correct PC

Backend(ROB)    FTQ             BPU             IFU             IBuffer
 |               |               |               |               |
 | [CN] Misprediction detected
 |--toFtq.redirect.valid(BranchPredictionRedirect: target, ftqIdx, ftqOffset)-->|
 |               |               |               |               |
 | [CN+1] needFlush = RegNext(redirect.valid)
 |               |--toIfu.redirect.valid------------------->|
 |               |               |               | f0/f1/f2/f3_flush
 |               |--icacheFlush---------------------------------------->(ICache)
 |               |--toBpu.redirect.valid-------->|
 |               |               | BPU: GHR/PHR rollback
 |               |               | s3/s2/s1_valid := false
 |               |               |               |--flush---------->|
 |               |               |               |               | full IBuffer flush
 |               |               |               |               |
 | [CN+2] BPU restarts
 |               |               | S0: redirect.target → new startPc
 |               |<--prediction(new start)-------|
 |               |               |               |
 | [CN+3] IFU restarts
 |               |--toIfu.req(new FetchRequestBundle)---------->|
 |               |               |               |
 |               |               |               | (waiting for new instructions)
"""

SEQ_D = r"""
Sequence Diagram D: ICache Miss (Stall)
========================================
  Context: ICache miss → IFU F2 stalls → FTQ back-pressure

FTQ             IFU             ICache          MissHandler
 |               |               |               |
 |         [C2] F2: ICache miss  |               |
 |               | f2_valid=1, f2_icache_all_resp_wire=0
 |               |--icacheStop := !f3_ready------>|
 |               |               |--miss req---->|
 |               |               |               | → L2 fetch
 |               |               |               |
 | [C2~X] F2 stall               |               |
 |               | f2_fire=0 (icacheRespAllValid=0)
 |<--fromFtq.req.ready=0---------|
 |  (FTQ stall: f1_ready=0)      |               |
 |               |               |               |
 | [CX] L2 responds, miss resolved               |
 |               |               |<--refill-------|
 |               |<--fetch.resp.valid=1-----------|
 |               | icacheRespAllValid=1, f2_fire=1
 |<--fromFtq.req.ready=1---------|
 |  (resume)     |               |               |
 |               | TopDown: icacheMissBubble → topdown_stages(1)
"""

SEQ_E = r"""
Sequence Diagram E: MMIO Fetch
================================
  Context: MMIO-region instruction fetch via InstrUncache, serialized per ROB commit

IFU(F3)         FTQ             InstrUncache    Backend(ROB)
 |               |               |               |
 | F2: f2_pmp_mmio detected
 | → F3: MMIO mode entered       |               |
 |               |               |               |
 | F3: MMIO fetch request        |               |
 |--toUncache.valid(InsUncacheReq: addr)-------->|
 |               |               |<--ready-------|
 |               |               | (latency L)   |
 |               |               |--fromUncache.valid(InsUncacheResp: data)-->|
 |               |               |               |
 | MMIO instr extracted (1 instruction at a time)
 |--mmioCommitRead.valid-------->|               |
 |               |               |               |
 | ROB commits the MMIO instruction              |
 |               |<--toFtq.commit.valid(ROB commit ptr)-------------------|
 |               |--mmioLastCommit (commit ptr >= mmioPtr)--------------->|
 |               |               |               |
 | [if mmioLastCommit=1]         |               |
 | mmio_redirect → next MMIO PC (flush F1/F2)
 |--pdWb(misOffset, target)----->|               |
 |               |               |               |
 | [else: wait (stall)]          |               |
"""

BPU_PIPELINE = r"""
BPU (Predictor) Pipeline — Internal Architecture
=================================================

NOTE: All predictors receive the SAME s0_startPc simultaneously.
      S1/S2/S3 results differ only in LOOKUP LATENCY, not in which PC is processed.

  Cycle T:   S0 (combinational)
  ─────────────────────────────────────────────────────────────────
  s0_startPc = MuxCase(s0_startPcReg, [
    redirect.valid  → redirect.bits.target,        # highest priority
    s3_override     → s3_prediction.target,
    s1_valid        → s1_prediction.target,
    else            → s0_startPcReg (hold)
  ])
  All predictors (ubtb, abtb, utage, uras, mbtb, tage, sc, ittage, ras)
    receive s0_startPc simultaneously.

  Cycle T+1: S1 (1-cycle latency results)
  ─────────────────────────────────────────────────────────────────
  Results available:
    uBTB  → 1 candidate (index 0), always-taken block BTB (32-entry fully-assoc)
    ABTB  → up to 8 candidates (index 1..8), block BTB (8-way set)
    uTAGE → direction correction for conditional branches
    uRAS  → fast return target (S1 early RAS)

  Combined candidate pool: s1_btbPrediction[0..8]
    [0] = uBTB output   [1..8] = ABTB outputs
    (Not competing — merged into one Vec, position-sorted)

  takenMask[i] = pred[i].valid &&
    (direct || indirect || (conditional && Mux(utageHit, utageTaken, pred.taken)))

  CompareMatrix(positions) → getLeastElementOH(takenMask)
    → s1_firstTakenBranchOH (1-hot, smallest cfiPosition among taken)
    → s1_prediction = Mux(s1_taken, firstTakenBranch, fallThrough)

  if s1_prediction.isReturn && uras.isCanUse:
    s1_prediction.target := uras.retTarget

  s1_fire → FTQ: prediction.valid=1, s3Override=0  (new entry enqueue)
  s1_prediction.target → next cycle s0_startPc

  Cycle T+2: S2 (2-cycle latency results)
  ─────────────────────────────────────────────────────────────────
  Results available:
    MBTB  → s2_mbtbResult (main BTB, more accurate, block BTB 32B half-block)
    TAGE  → direction prediction (8 tables, different history lengths)
    SC    → Statistical Corrector override (threshold-gated)

  s2_condTakenMask: apply tage/sc direction to mbtb entries
    Mux(sc.scUsed → sc.scTaken, tage.useProvider → tage.pred, else → mbtb.taken)
  s2_takenMask = s2_condTakenMask || jumpMask (direct/indirect always taken)

  S2 → NOT sent to FTQ. Passes to S3 only.
  s2_flush = s3_flush || s3_override  (BPU internal flush)

  Cycle T+3: S3 (3-cycle latency results)
  ─────────────────────────────────────────────────────────────────
  Results available:
    ITTAGE  → indirect branch target
    RAS     → return address (full speculative RAS)

  s3_mbtbResult   = RegEnable(s2_mbtbResult, s2_fire)  (latched from S2)
  s3_takenMask    = RegEnable(s2_takenMask, s2_fire)
  s3_s1Prediction = RegEnable(RegEnable(s1_prediction, s1_fire), s2_fire)

  target priority:
    1. ras.topRetAddr   (if isReturn)
    2. ittage.target    (if needIttage && ittage.hit)
    3. mbtb.target      (if taken)
    4. fallThrough

  s3_override = s3_valid && !(s3_prediction === s3_s1Prediction)
  if s3_override:
    → FTQ: prediction.valid=1, s3Override=1  (existing entry update)
    → s2_flush=1 (kill BPU in-flight S1+S2)
    → s0_startPc := s3_prediction.target (2nd priority MUX)
"""

IFU_PIPELINE = r"""
NewIFU Internal Pipeline
=========================

  F0 (combinational — launch phase)
  ──────────────────────────────────
  fromFtq.req.valid → f0_valid
  f0_doubleLine = startAddr[blockOffBits-1] === 1  (cross-cacheline fetch)
  f0_vSetIdx = [get_idx(startAddr), get_idx(nextlineStart)]
  f0_fire = fromFtq.req.fire = f0_valid && f1_ready && icacheReady
  f0_flush_from_bpu = shouldFlushByStage2(ftqIdx) || shouldFlushByStage3(ftqIdx)
  Note: FTQ sends req directly to ICache; IFU receives only the response.

  F1 (1 register stage — PC calculation)
  ────────────────────────────────────────
  f1_valid  = RegInit(false.B)
  f1_ftq_req = RegEnable(f0_ftq_req, f0_fire)
  PC adder optimization (PcCutPoint = VAddrBits/4 - 1):
    f1_pc_lower_result(i) = Cat(0.U(1.W), startAddr[PcCutPoint-1:0]) + (i*2).U
    f1_pc = CatPC(f1_pc_lower_result, f1_pc_high, f1_pc_high_plus1)
  f1_cut_ptr(i) = startAddr[blockOffBits-1:1] + i  (ICache data slice pointer)
  f1_fire = f1_valid && f2_ready

  F2 (1 register stage — ICache response + Predecode)
  ─────────────────────────────────────────────────────
  f2_valid = RegInit(false.B)
  Wait for ICache response: f2_icache_all_resp_wire (timing-critical path)
    = fromICache.valid
      && fromICache.bits.vaddr[0] === f2_ftq_req.startAddr
      && (!f2_doubleLine || fromICache.bits.vaddr[1] === f2_ftq_req.nextlineStart)

  Data extraction:
    f2_data_2_cacheline = Cat(fromICache.bits.data, fromICache.bits.data)
    f2_cut_data = cut(f2_data_2_cacheline, f2_cut_ptr)  (PredictWidth+1 × 16-bit slices)

  Exception generation:
    f2_exception = ExceptionType.merge(iTLB_exception, mmio_mismatch_exception)
    Priority: iTLB(PF/GPF/AF) > PMP(AF) > ECC(AF)

  Predecode (combinational):
    preDecoder.in = {f2_cut_data, f2_pc, frontendTrigger}
    f2_pd = preDecoder.out.pd  (PreDecodeInfo per instruction)
    f2_instr_range = jump_range & ftr_range

  f2_fire = f2_valid && f3_ready && icacheRespAllValid

  F3 (1 register stage — RVC expand + PredChecker + IBuffer send)
  ─────────────────────────────────────────────────────────────────
  f3_valid = RegInit(false.B)
  RVC expansion: expanders(i): RVCExpander → f3_expd_instr(i)
  F3Predecoder: finalize brType/isCall/isRet
  PredChecker: compare BPU prediction vs actual predecode result
    Faults detected:
      jalFault, jalrFault, retFault, targetFault, notCFIFault, invalidTakenFault
    → wb_redirect=true → IFU sends correction to FTQ

  MMIO handling (if f3_pmp_mmio):
    → MMIO FSM → toUncache.valid=true
    → mmio_redirect → f1/f2 flush
    → wait mmioLastCommit → next MMIO fetch

  Outputs:
    io.toIbuffer.valid = f3_valid && !mmio_state
    toFtq.pdWb.valid   = (predecode writeback)

  Flush propagation:
    f3_flush = backend_redirect || (wb_redirect && !f3_wb_not_flush)
    f2_flush = backend_redirect || mmio_redirect || wb_redirect
    f1_flush = f2_flush
    f0_flush = f1_flush || f0_flush_from_bpu
"""

FTQ_POINTERS = r"""
FTQ Pointer Structure
======================
  Invariant: commPtr <= ifuWbPtr <= ifuPtr <= bpuPtr

  bpuPtr  ──► [X] BPU enqueue position
  ifuPtr  ──► [Y] Next entry to send to IFU
  ifuWbPtr──► [Z] IFU predecode writeback confirmed position
  commPtr ──► [W] Last committed entry

  entry_fetch_status:  f_to_send (0) / f_sent (1)
  commitStateQueue:    c_empty / c_toCommit / c_committed / c_flushed  (2-bit per slot)

  Circular queue size: FtqSize=64 entries
  Each entry covers one fetch block (64B, up to 16 instructions)
"""

IBUF_STRUCTURE = r"""
IBuffer Bank Layout (Interleaved)
===================================
  IBufSize=48, IBufNBank=6, bankSize=8

  Linear index:  0  1  2  3  4  5  6  7  8  9  10  11  12 ...
  Bank mapping:
    Bank 0: ibuf[0, 6, 12, 18, 24, 30, 36, 42]
    Bank 1: ibuf[1, 7, 13, 19, 25, 31, 37, 43]
    Bank 2: ibuf[2, 8, 14, 20, 26, 32, 38, 44]
    Bank 3: ibuf[3, 9, 15, 21, 27, 33, 39, 45]
    Bank 4: ibuf[4,10, 16, 22, 28, 34, 40, 46]
    Bank 5: ibuf[5,11, 17, 23, 29, 35, 41, 47]

  Dequeue 2-stage read:
    Stage 1: each bank selects 1 entry (bankSize:1 Mux per bank)
    Stage 2: each output slot selects from IBufNBank candidates (IBufNBank:1 Mux)
    → DecodeWidth=6 reads, each from a different bank → no port conflict

  Bypass path: enqPtr==deqPtr && decodeCanAccept
    → IFU data forwarded directly to OutputEntries (skip ibuf registers)
    → saves 1 cycle when buffer is empty

  Full detection:
    allowEnq = (IBufSize - PredictWidth) >= numValidNext
    (PredictWidth=16 margin to prevent overflow)
"""

BTB_HIERARCHY = r"""
BTB/FTB Hierarchy (KunMingHu v3)
==================================
  This version evolves from uFTB→FTB (2-tier) to uBTB→ABTB→MBTB (3-tier).
  All BTBs are BLOCK BTBs: one entry covers one fetch block.

  +──────────────────────────────────────────────────────────+
  | Stage | BTB   | Direction  | Latency | Entry Coverage    |
  +──────────────────────────────────────────────────────────+
  | S1    | uBTB  | uTAGE      | 1 cycle | 64B fetch block   |
  |       | ABTB  | uTAGE      | 1 cycle | 64B fetch block   |
  +──────────────────────────────────────────────────────────+
  | S2    | MBTB  | TAGE + SC  | 2 cycle | 32B half-block    |
  +──────────────────────────────────────────────────────────+
  | S3    | —     | ITTAGE+RAS | 3 cycle | (target refine)   |
  +──────────────────────────────────────────────────────────+

  uBTB: 32 entries, fully-associative, tag=PC[22:1], always-taken
  ABTB: 8-way set, tag=PC[24:1], up to 8 candidates per lookup
  MBTB: 2 AlignBanks × 4 ways, tag=PC[31:16], up to 8 entries per 32B half-block
"""


# ─── Korean → English translation map ────────────────────────────────────────

KO_EN = {
    "역할": "Role",
    "위치": "Location",
    "Pipeline stage 수": "Pipeline stages",
    "파라미터": "Parameter",
    "영향": "Effect",
    "설명": "Description",
    "방향": "Direction",
    "조건": "Condition",
    "동작": "Behavior",
    "코드 근거": "Code reference",
    "내부 서브모듈": "Internal submodules",
    "서브모듈": "Submodule",
    "예외 종류": "Exception type",
    "감지 시점": "Detection point",
    "처리 방법": "Handling",
    "예외 정보": "Exception info",
    "저장 형태": "Storage form",
    "전달 방법": "Delivery method",
    "타입": "Type",
    "크기": "Size",
    "포인터": "Pointer",
    "이름": "Name",
    "모듈": "Module",
    "Critical Path 후보": "Critical path candidate",
    "상황": "Situation",
    "처리": "Handling",
    "불변식": "Invariant",
}

def translate_ko(text):
    """Replace known Korean terms with English. Attempt simple translation of common patterns."""
    # Replace known terms
    for ko, en in KO_EN.items():
        text = text.replace(ko, en)
    return text


# ─── Main document builder ────────────────────────────────────────────────────

def build_doc():
    doc = Document()

    # Title
    doc.add_heading("XiangShan Frontend Architecture Documentation", 0)
    p = doc.add_paragraph(
        "Branch: KunMingHu v3  |  Last updated: 2026-02-27\n"
        "Sources: Frontend.scala, Bpu.scala, NewFtq.scala, IFU.scala, "
        "IBuffer.scala, and sub-module files under bpu/"
    )

    doc.add_page_break()

    # ── 1. Block Diagram Overview ─────────────────────────────────────────────
    doc.add_heading("1. Frontend Block Diagram Overview", 1)

    doc.add_heading("1.1 Block Summary", 2)
    doc.add_paragraph(
        "XiangShan Frontend is a speculative instruction-supply pipeline: "
        "BPU (Branch Predictor) → FTQ (Fetch Target Queue) → IFU (Instruction Fetch Unit) "
        "→ IBuffer (Instruction Buffer) → Backend (Decode). "
        "It continuously supplies decoded instructions to the backend while tolerating "
        "ICache misses, branch mispredictions, and TLB faults."
    )
    bullets = [
        "Main inputs: Backend redirect (misprediction flush), sfence, tlbCsr, csrCtrl",
        "Main outputs: io.backend.cfVec (DecodeWidth CtrlFlow entries, IBuffer→Decode), "
        "io.backend.stallReason (stall reason for TopDown analysis)",
        "Performance bottlenecks: ICache miss (IFU stall), FTQ full (BPU back-pressure), "
        "IBuffer full (fetch stall), misprediction redirect flush",
    ]
    for b in bullets:
        p = doc.add_paragraph(style='List Bullet')
        p.add_run(b)

    doc.add_heading("1.2 Key Parameters", 2)
    params = [
        ["Parameter", "Source", "Default", "Effect"],
        ["FtqSize", "FtqParameters.FtqSize", "64", "FTQ entry count (BPU-IFU buffer depth)"],
        ["IBuffer.Size", "IBufferParameters.Size", "48", "Total IBuffer entries"],
        ["NumWriteBank", "IBufferParameters.NumWriteBank", "4", "IBuffer write banks"],
        ["NumReadBank", "IBufferParameters.NumReadBank", "8", "IBuffer read banks (≥ DecodeWidth)"],
        ["FetchBlockInstNum", "FetchBlockSize(64B)/instBytes", "16 or 32", "Max instructions per fetch block"],
        ["DecodeWidth", "XSCoreParamsKey.DecodeWidth", "6", "IBuffer→Decode output width"],
        ["PhrHistoryLength", "FrontendParameters.getPhrHistoryLength", "computed", "Path History Register length"],
        ["GhrHistoryLength", "HasBpuParameters.GhrHistoryLength", "SC max table", "Global History Register for SC"],
        ["ResolveEntryBranchNumber", "FrontendParameters", "8", "Max branch slots per FTQ resolve entry"],
    ]
    add_table_from_rows(doc, params)

    doc.add_heading("1.3 Top-Level Block Diagram", 2)
    add_code_block(doc, BLOCK_DIAGRAM)

    doc.add_heading("1.4 BPU Internal S3 Override", 2)
    doc.add_paragraph(
        "When S3's result differs from S1's result for the same fetch PC, "
        "s3_override=true is asserted (Bpu.scala: "
        "s3_valid && !(s3_prediction === s3_s1Prediction)). "
        "The FTQ receives the corrected S3 prediction and updates the existing entry "
        "(io.toFtq.prediction.bits.s3Override := s3_override). "
        "There is no S2-direct override; when s3_flush fires, S2/S1 are also flushed. "
        "FTQ relays flushFromBpu to IFU, which consumes it in F0."
    )

    doc.add_heading("1.5 Exception / Error Paths", 2)
    exc_rows = [
        ["Path", "Detection", "Handling"],
        ["iTLB Page Fault (PF)", "ICache → iTLB → ExceptionType.pf", "Propagated via fromICache.bits.exception → IFU F2 → IBuffer → Decode"],
        ["iTLB Guest PF (GPF)", "iTLB response", "Same path as PF"],
        ["PMP Access Fault (AF)", "ExceptionType.fromPMPResp", "Merged into ICache response → same path"],
        ["ECC / TileLink corrupt (AF)", "ExceptionType.fromECC / fromTilelink", "icache.io.error → errorReg → io.error → L1BusErrorUnit"],
        ["MMIO instruction", "IFU F3: f3_pmp_mmio", "InstrUncache path (64-bit per fetch); mmio_redirect → F2/F3 flush"],
        ["Cross-page RVI", "F2: last-in-line 32-bit spanning page boundary", "CrossPF/GPF/AF exception set in IBuffer"],
    ]
    add_table_from_rows(doc, exc_rows)
    doc.add_paragraph("Exception priority (ExceptionType.merge): iTLB(PF/GPF/AF) > PMP(AF) > ECC(AF)")

    doc.add_heading("1.6 Timing Hints", 2)
    timing_rows = [
        ["Module", "Critical Path", "Notes"],
        ["IFU F1", "PC adder", "f1_pc_lower_result: 16 parallel adders (CatPC optimization)"],
        ["IFU F2", "ICache resp addr match", "fromICache.bits.vaddr[0] === f2_ftq_req.startAddr — timing critical (IFU.scala:366)"],
        ["BPU S2/S3", "TAGE/SC multi-table lookup", "Multiple folded history XOR + SRAM reads"],
        ["FTQ", "FtqPtr comparison", "isAfter / isBefore on 64-entry circular queue"],
        ["IBuffer", "enqOffset PopCount", "enqOffset = PopCount(io.in.bits.valid.take(i)), PredictWidth=16 wide"],
        ["BPU→IFU", "flushFromBpu.shouldFlushByStage2/3", "FtqPtr comparison, must be consumed without delay in IFU F0"],
    ]
    add_table_from_rows(doc, timing_rows)

    doc.add_page_break()

    # ── 2. I/O Sequence Diagrams ──────────────────────────────────────────────
    doc.add_heading("2. Frontend I/O Sequence Diagrams", 1)

    doc.add_heading("2.1 I/O Summary", 2)
    doc.add_heading("2.1.1 Inputs (FrontendInlinedImp.io)", 3)
    in_rows = [
        ["Port", "Protocol", "Source", "Description"],
        ["io.backend.toFtq.redirect", "Valid", "Backend → FTQ", "Misprediction/MemVio redirect"],
        ["io.backend.toFtq.commit", "Valid FtqPtr", "Backend → FTQ", "ROB commit signal (update FTQ commit ptr, MMIO lastCommit check)"],
        ["io.backend.canAccept", "Bool", "Backend → IBuffer", "Decode can accept"],
        ["io.backend.wfi.wfiReq", "Bool", "Backend → ICache/Uncache", "WFI request"],
        ["io.reset_vector", "UInt", "SoC → BPU", "Reset start PC"],
        ["io.sfence", "Bundle", "CSR → iTLB", "TLB flush"],
        ["io.tlbCsr", "Bundle", "CSR → iTLB/BPU", "SATP/STATUS etc."],
        ["io.csrCtrl", "Bundle", "CSR → modules", "bp_ctrl, frontend_trigger, pf_ctrl etc."],
        ["io.ptw", "TlbPtwIO", "PTW → iTLB", "Page table walk response"],
        ["io.softPrefetch", "Valid Vec", "Backend → ICache", "Software prefetch request"],
    ]
    add_table_from_rows(doc, in_rows)

    doc.add_heading("2.1.2 Outputs", 3)
    out_rows = [
        ["Port", "Protocol", "Destination", "Description"],
        ["io.backend.cfVec", "DecoupledIO Vec", "Frontend → Decode", "DecodeWidth CtrlFlow instructions"],
        ["io.backend.fromFtq", "Bundle", "FTQ → Backend", "PC mem write, newest entry etc."],
        ["io.backend.fromIfu", "Bundle", "IFU → Backend", "gpaddr mem write"],
        ["io.backend.wfi.wfiSafe", "Bool", "Frontend → Backend", "WFI entry safe"],
        ["io.error", "L1BusErrorUnitInfo", "ICache → Backend", "ECC/bus error"],
        ["io.frontendInfo.ibufFull", "Bool", "IBuffer → Backend", "IBuffer full status"],
        ["io.frontendInfo.bpuInfo", "Bundle", "FTQ → Backend", "BPU hit/miss counters"],
    ]
    add_table_from_rows(doc, out_rows)

    doc.add_heading("2.2 Group A: Normal Fetch (ICache Hit)", 2)
    doc.add_paragraph(
        "BPU prediction → FTQ enqueue → IFU fetch → ICache hit → IBuffer enqueue → Decode delivery"
    )
    add_code_block(doc, SEQ_NORMAL)

    doc.add_heading("2.3 Group B: BPU S3 Override", 2)
    doc.add_paragraph(
        "BPU uses two independent paths to FTQ:\n"
        "  (1) S1 new enqueue: s1_fire → FTQ entryQueue[bpuPtr] new write, bpuPtr++\n"
        "  (2) S3 override: s3_override → entryQueue[s3FtqPtr] overwrite, bpuPtr := s3FtqPtr+1\n\n"
        "S1 (uBTB/ABTB) prediction is always sent to FTQ first. "
        "S3 override occurs only when S3 result != S1 result, performing a post-correction. "
        "S2 only causes BPU-internal flush (not sent to FTQ). "
        "s3_override is independent of prediction.ready (bpuS3Redirect path)."
    )

    doc.add_heading("FTQ Index Tracking (s3FtqPtr)", 3)
    ftq_idx_rows = [
        ["Stage", "Code reference", "Description"],
        ["S1 fire", "s2_ftqPtr = RegEnable(io.fromFtq.bpuPtr, s1_fire)", "Latch bpuPtr at S1 fire into s2"],
        ["S2 fire", "s3_ftqPtr = RegEnable(s2_ftqPtr, s2_fire)", "Forward s2_ftqPtr to S3"],
        ["S3", "io.toFtq.s3FtqPtr := s3_ftqPtr", "Send override target index to FTQ"],
        ["Override", "predictionPtr = s3Override ? s3FtqPtr : bpuPtr(0)", "Update entryQueue(s3FtqPtr)"],
        ["bpuPtr rollback", "when(s3Override) { bpuPtr := s3FtqPtr + 1 }", "Move enqueue pointer past override entry"],
    ]
    add_table_from_rows(doc, ftq_idx_rows)

    doc.add_heading("FTQ Override Behavior Summary", 3)
    ovr_rows = [
        ["Condition", "Action", "Code reference"],
        ["prediction.fire (S1, not s3Override)", "entryQueue(bpuPtr) new write, bpuPtr+1", "prediction.fire"],
        ["s3Override=1", "entryQueue(s3FtqPtr) overwrite, bpuPtr := s3FtqPtr+1", "bpuS3Redirect, predictionPtr"],
        ["ifuPtr >= s3FtqPtr", "ifuPtr := s3FtqPtr (rollback)", "when(ifuPtr >= ftqIdx)"],
        ["pfPtr >= s3FtqPtr", "pfPtr := s3FtqPtr (rollback)", "when(pfPtr >= ftqIdx)"],
        ["IFU stage idx >= s3FtqPtr", "shouldFlushByStage3=true → flush", "!isAfter(s3FtqPtr, idxToFlush)"],
    ]
    add_table_from_rows(doc, ovr_rows)

    doc.add_heading("2.3.1 B-1: Standard S3 Override (IFU already ahead)", 3)
    add_code_block(doc, SEQ_B1)

    doc.add_heading("2.3.2 B-2: Early S3 Override (IFU still behind)", 3)
    add_code_block(doc, SEQ_B2)

    doc.add_heading("2.3.3 B-3: Back-pressure S3 Override (prediction.ready=0)", 3)
    add_code_block(doc, SEQ_B3)

    doc.add_heading("2.4 Group C: Backend Redirect (Misprediction Flush)", 2)
    doc.add_paragraph(
        "ROB detects misprediction → full Frontend flush → restart from correct PC."
    )
    add_code_block(doc, SEQ_C)

    doc.add_heading("2.5 Group D: ICache Miss (Stall)", 2)
    doc.add_paragraph("ICache miss → IFU F2 stall → FTQ back-pressure.")
    add_code_block(doc, SEQ_D)

    doc.add_heading("2.6 Group E: MMIO Fetch", 2)
    doc.add_paragraph("MMIO-region instruction fetch via InstrUncache, serialized per ROB commit.")
    add_code_block(doc, SEQ_E)

    doc.add_heading("2.7 Edge Cases", 2)
    edge_rows = [
        ["Case", "Behavior"],
        ["IBuffer full → fetch stall", "allowEnq=false → io.in.ready=false → IFU stall → F3 stall → ICache stall → FTQ back-pressure"],
        ["FTQ full → BPU stall", "ftqFullStall → s3_ready=false → s2/s1 stall chain → s0 PC freeze"],
        ["Cross-cacheline fetch", "f0_doubleLine=1 → ICache fetches two cachelines; F2 combines with Cat(data,data)+cut()"],
        ["Cross-page RVI exception", "F2: last-in-line 32-bit instruction; CrossPF/GPF/AF set in IBuffer"],
        ["Flush classification (TopDown)", "ControlBTBMissBubble / TAGEMissBubble / SCMissBubble / ITTAGEMissBubble / RASMissBubble"],
    ]
    add_table_from_rows(doc, edge_rows)

    doc.add_page_break()

    # ── 3. BPU (Predictor) Analysis ───────────────────────────────────────────
    doc.add_heading("3. Predictor (BPU) Analysis", 1)

    doc.add_heading("3.1 Module Summary", 2)
    doc.add_paragraph(
        "Role: 3-stage branch prediction pipeline. "
        "S1 (uBTB+ABTB+uTAGE), S2 (MBTB+TAGE+SC), S3 (MBTB-latched+ITTAGE+RAS). "
        "Each stage sends predictions to FTQ; a later stage overrides FTQ if its result differs from S1. "
        "Location: Frontend.scala → Module(new Predictor) inside FrontendInlinedImp. "
        "Pipeline: 3 register stages (S1/S2/S3); S0 is the combinational launch phase."
    )

    doc.add_heading("3.2 Key Parameters", 2)
    bpu_params = [
        ["Parameter", "Source", "Default", "Effect"],
        ["numDup", "HasBPUConst.numDup", "4", "PC/history register copies (timing closure)"],
        ["HistoryLength", "XSCoreParamsKey.HistoryLength", "256", "Global history register width"],
        ["numBr", "XSCoreParamsKey.numBr", "2", "Branch slots per FTB entry"],
        ["totalSlot", "numBr", "2", "Total slots (brSlots + tailSlot)"],
        ["MaxMetaLength", "HasBPUConst.MaxMetaLength", "512", "Prediction metadata storage bits"],
        ["FtqSize", "XSCoreParamsKey.FtqSize", "64", "FTQ size (BPU stall threshold)"],
        ["numBpStages", "constant", "3", "Pipeline stage count"],
    ]
    add_table_from_rows(doc, bpu_params)

    doc.add_heading("3.3 Interfaces", 2)
    bpu_iface = [
        ["Port", "Dir", "Protocol", "Description"],
        ["io.bpu_to_ftq.resp", "out", "Decoupled", "Prediction result → FTQ (s1/s2/s3)"],
        ["io.ftq_to_bpu.redirect", "in", "Valid", "FTQ→BPU redirect (misprediction)"],
        ["io.ftq_to_bpu.update", "in", "Valid", "FTQ→BPU training data"],
        ["io.ftq_to_bpu.enq_ptr", "in", "—", "FTQ current enqueue pointer"],
        ["io.ctrl", "in", "—", "BPUCtrl: enable/disable per predictor"],
        ["io.reset_vector", "in", "—", "Reset PC"],
    ]
    add_table_from_rows(doc, bpu_iface)

    doc.add_heading("3.4 BPU Pipeline Architecture", 2)
    add_code_block(doc, BPU_PIPELINE)

    doc.add_heading("3.5 BTB / FTB Hierarchy", 2)
    add_code_block(doc, BTB_HIERARCHY)

    doc.add_heading("3.6 uBTB (Micro BTB) — S1 Stage", 2)
    ubtb_rows = [
        ["Property", "Value"],
        ["BTB type", "Block BTB — tag = fetch block start PC[22:1], 1 entry per fetch block"],
        ["Structure", "Fully Associative Cache"],
        ["Entries", "32"],
        ["Tag width", "22 bits"],
        ["Target width", "22 bits (2B aligned)"],
        ["Useful counter", "2-bit saturating (signed, SaturateNegative = invalid)"],
        ["Replacement", "PLRU (least-useful first)"],
        ["History", "None"],
        ["Prediction latency", "1 cycle"],
        ["Behavior", "Always-taken predictor — if hit, taken=true"],
    ]
    add_table_from_rows(doc, ubtb_rows)

    doc.add_heading("3.7 Backpressure / Flush Control", 2)
    bpu_flow_rows = [
        ["Condition", "Behavior", "Code reference"],
        ["FTQ full (prediction.ready=0)", "s1_fire=0 → S1 stall, S0 stall cascade", "s1_fire := s1_valid && s2_ready && prediction.ready"],
        ["s3_override", "s2_flush=1, s1_flush=1 → BPU internal S1+S2 kill", "s2_flush := s3_flush || s3_override"],
        ["redirect.valid", "s3_flush=1 → s3_valid cleared, cascade to s2/s1", "s3_flush := redirect.valid"],
        ["s0_stall", "s0_startPcReg held (PC freeze)", "s0_stall := !(s1_valid || s3_override || redirect.valid)"],
        ["s3_override + FTQ full", "bpuS3Redirect executes regardless of prediction.ready", "Ftq.scala: bpuS3Redirect = prediction.valid && s3Override"],
    ]
    add_table_from_rows(doc, bpu_flow_rows)

    doc.add_page_break()

    # ── 4. NewIFU Analysis ─────────────────────────────────────────────────────
    doc.add_heading("4. NewIFU Analysis", 1)

    doc.add_heading("4.1 Module Summary", 2)
    doc.add_paragraph(
        "Role: Fetches instructions from ICache using the fetch address supplied by FTQ, "
        "performs predecode, and delivers results to IBuffer. "
        "MMIO instructions are handled separately via InstrUncache. "
        "Location: Frontend.scala → Module(new NewIFU) inside FrontendInlinedImp. "
        "Pipeline: 3 register stages (F1/F2/F3) + F0 combinational launch phase."
    )

    doc.add_heading("4.2 Key Parameters", 2)
    ifu_params = [
        ["Parameter", "Source", "Default", "Effect"],
        ["PredictWidth", "HasXSParameter.PredictWidth", "16", "Max half-words per fetch block"],
        ["HasCExtension", "HasXSParameter.HasCExtension", "true", "RVC (16-bit) instruction support"],
        ["CommitWidth", "XSCoreParamsKey.CommitWidth", "6", "rob_commits port count"],
        ["fetchQueueSize", "HasIFUConst.fetchQueueSize", "2", "MMIO fetch internal queue size"],
        ["blockOffBits", "HasICacheParameters", "6", "Cache line offset bits (64B → 6 bits)"],
        ["mmioBusWidth", "HasInstrMMIOConst", "64", "MMIO fetch bus width (bits)"],
    ]
    add_table_from_rows(doc, ifu_params)

    doc.add_heading("4.3 Interfaces", 2)
    ifu_iface = [
        ["Port", "Dir", "Protocol", "Description"],
        ["io.ftqInter.fromFtq.req", "in", "Decoupled", "FTQ→IFU fetch request (FetchRequestBundle)"],
        ["io.ftqInter.fromFtq.redirect", "in", "Valid", "Backend redirect"],
        ["io.ftqInter.fromFtq.flushFromBpu", "in", "—", "BPU S2/S3 flush info"],
        ["io.ftqInter.toFtq.pdWb", "out", "Valid", "Predecode writeback → FTQ"],
        ["io.icacheInter.resp", "in", "ValidIO", "ICache→IFU response"],
        ["io.icacheInter.icacheReady", "in", "Bool", "ICache ready (F0 req.ready condition)"],
        ["io.icacheStop", "out", "Bool", "ICache stop request (!f3_ready)"],
        ["io.toIbuffer", "out", "Decoupled", "IFU→IBuffer instruction delivery"],
        ["io.uncacheInter.toUncache", "out", "Decoupled", "MMIO fetch request"],
        ["io.uncacheInter.fromUncache", "in", "Decoupled", "MMIO fetch response"],
        ["io.iTLBInter", "in/out", "—", "MMIO re-translate iTLB (blockable)"],
        ["io.pmp", "in/out", "—", "PMP check (last IFU port)"],
    ]
    add_table_from_rows(doc, ifu_iface)

    doc.add_heading("4.4 Internal Pipeline", 2)
    add_code_block(doc, IFU_PIPELINE)

    doc.add_heading("4.5 Submodules", 2)
    sub_rows = [
        ["Submodule", "Role"],
        ["PreDecode", "Find instruction boundaries in 16-bit half-word stream; RVC detection; branch type first pass"],
        ["F3Predecoder", "Finalize brType/isCall/isRet in F3"],
        ["PredChecker", "Compare BPU prediction vs actual predecode; generate IFU redirect if mismatch"],
        ["FrontendTrigger", "Hardware debug triggers"],
        ["RVCExpander × PredictWidth", "Expand 16-bit RVC to 32-bit"],
    ]
    add_table_from_rows(doc, sub_rows)

    doc.add_heading("4.6 Flush Propagation", 2)
    flush_rows = [
        ["Condition", "Behavior"],
        ["IBuffer full (!toIbuffer.ready)", "f3_fire=0, f3_valid held; icacheStop=!f3_ready → ICache stall"],
        ["ICache not ready (!icacheReady)", "fromFtq.req.ready=0 → FTQ stall"],
        ["ICache miss (!icacheRespAllValid)", "f2_fire=0, f2_valid held → f1_ready=0 → FTQ stall"],
        ["Backend redirect", "f0~f3 flush, f1/f2/f3_valid=0"],
        ["BPU S2/S3 flush", "f0_flush_from_bpu → f0 flush only (F1+ preserved)"],
        ["wb_redirect", "f2/f3 flush; correction info sent to FTQ"],
        ["MMIO redirect", "f1/f2 flush; F3 occupied until MMIO complete"],
    ]
    add_table_from_rows(doc, flush_rows)

    doc.add_heading("4.7 Exceptions", 2)
    ifu_exc_rows = [
        ["Exception", "Detection", "Handling"],
        ["Page Fault (PF)", "F2: fromICache.bits.exception (iTLB result)", "ExceptionType.pf → FetchToIBuffer.exceptionType"],
        ["Guest Page Fault (GPF)", "F2: iTLB response", "ExceptionType.gpf → same path"],
        ["Access Fault (AF)", "F2: PMP/ECC/TileLink corrupt", "ExceptionType.af → same path"],
        ["MMIO mismatch", "F2: double-line pmp_mmio/itlb_pbmt mismatch", "AF on second line"],
        ["Cross-page exception", "F2: last-in-line RVI crossing page", "CrossPF/GPF/AF in IBuffer"],
        ["Illegal RVC", "F3: expander.io.ill", "IBufferExceptionType.rvcII"],
    ]
    add_table_from_rows(doc, ifu_exc_rows)

    doc.add_page_break()

    # ── 5. FTQ Analysis ────────────────────────────────────────────────────────
    doc.add_heading("5. FTQ (Fetch Target Queue) Analysis", 1)

    doc.add_heading("5.1 Module Summary", 2)
    doc.add_paragraph(
        "Role: Central queue that buffers BPU predictions and supplies fetch requests "
        "to IFU/ICache/Prefetch in order. Receives IFU predecode writeback and Backend "
        "commit/redirect to update/restore BPU. "
        "Location: Frontend.scala → Module(new Ftq) inside FrontendInlinedImp. "
        "Structure: Pointer-based circular queue with SRAM storage — no register pipeline stages."
    )

    doc.add_heading("5.2 Key Parameters", 2)
    ftq_params = [
        ["Parameter", "Source", "Default", "Effect"],
        ["FtqSize", "XSCoreParamsKey.FtqSize", "64", "Max in-flight fetch blocks (circular)"],
        ["PredictWidth", "HasXSParameter.PredictWidth", "16", "commitStateQueue slots per entry"],
        ["copyNum", "Ftq.copyNum", "5", "Pointer copies (reduce fanout)"],
        ["FtqRedirectAheadNum", "HasXSParameter", "~4", "BjuCnt-based redirect ahead ports"],
    ]
    add_table_from_rows(doc, ftq_params)

    doc.add_heading("5.3 Interfaces", 2)
    ftq_iface = [
        ["Port", "Dir", "Protocol", "Description"],
        ["io.fromBpu.resp", "in", "Decoupled", "BPU prediction result"],
        ["io.toBpu.redirect", "out", "Valid", "Misprediction → BPU redirect"],
        ["io.toBpu.update", "out", "Valid", "Commit info → BPU training"],
        ["io.toBpu.enq_ptr", "out", "—", "Current bpuPtr"],
        ["io.fromIfu.pdWb", "in", "Valid", "IFU predecode writeback"],
        ["io.toIfu.req", "out", "Decoupled", "Fetch request to IFU"],
        ["io.toIfu.redirect", "out", "Valid", "IFU flush"],
        ["io.toIfu.flushFromBpu", "out", "—", "BPU S2/S3 override flush info"],
        ["io.toICache.req", "out", "Decoupled", "ICache fetch request (5 copies)"],
        ["io.fromBackend", "in", "CtrlToFtqIO", "redirect, rob_commits, ftqIdxAhead etc."],
        ["io.toBackend", "out", "FtqToCtrlIO", "pc_mem write, newest_entry info"],
        ["io.icacheFlush", "out", "Bool", "ICache pipeline flush"],
    ]
    add_table_from_rows(doc, ftq_iface)

    doc.add_heading("5.4 Pointer Structure", 2)
    add_code_block(doc, FTQ_POINTERS)

    doc.add_heading("5.5 Internal SRAM / Register Structures", 2)
    sram_rows = [
        ["Name", "Type", "Size", "Role"],
        ["ftq_pc_mem", "SyncDataModuleTemplate", "FtqSize × Ftq_RF_Components", "PC/nextLine storage, multiple read ports"],
        ["ftq_redirect_mem", "SyncDataModuleTemplate", "FtqSize × Ftq_Redirect_SRAMEntry", "Branch history/RAS state (redirect recovery)"],
        ["ftq_meta_mem", "SyncDataModuleTemplate", "FtqSize × MetaEntry", "BPU metadata + FTBEntry (training)"],
        ["ftq_pd_mem", "SyncDataModuleTemplate", "FtqSize × Ftq_pd_Entry", "IFU predecode result (brMask, jmpInfo)"],
        ["ftb_entry_mem", "SyncDataModuleTemplate", "FtqSize × FTBEntry_FtqMem", "FTB entry (redirect validation)"],
        ["update_target", "Reg Vec", "FtqSize × VAddrBits", "Predicted target addresses"],
        ["cfiIndex_vec", "Reg Vec", "FtqSize × ValidUInt", "CFI position index"],
        ["commitStateQueueReg", "RegInit Vec", "FtqSize × Vec(PW, 2bit)", "Per-slot commit state"],
        ["entry_fetch_status", "RegInit Vec", "FtqSize × 1bit", "f_to_send / f_sent"],
    ]
    add_table_from_rows(doc, sram_rows)

    doc.add_heading("5.6 Key Mechanisms", 2)

    doc.add_heading("BPU Enqueue 1-Cycle Delay", 3)
    doc.add_paragraph(
        "To break the critical path, entry_fetch_status / cfiIndex_vec / update_target are "
        "updated one cycle after BPU enqueue (last_cycle_bpu_in = RegNext(bpu_in_fire)). "
        "A bypass path (bpu_in_bypass_buf) supplies the just-enqueued PC directly to IFU "
        "when bpu_in_bypass_ptr === ifuPtr, avoiding SRAM read latency."
    )

    doc.add_heading("FTQ Full Detection", 3)
    doc.add_paragraph(
        "validEntries = distanceBetween(bpuPtr, commPtr). "
        "When validEntries >= FtqSize → new_entry_ready=false → io.fromBpu.resp.ready=false "
        "→ BPU back-pressure. "
        "Even when FTQ full, if canCommit fires, enqueue is allowed (deadlock prevention)."
    )

    doc.add_heading("5.7 Backpressure / Flush Control", 2)
    ftq_flow_rows = [
        ["Condition", "Behavior"],
        ["FTQ full", "io.fromBpu.resp.ready=false → BPU stall"],
        ["Backend redirect", "allowBpuIn/ToIfu=false (2 cycles), bpuPtr/ifuPtr restored"],
        ["BPU S2/S3 override", "bpuPtr rollback, ifuPtr rollback (if needed), flushFromBpu sent"],
        ["IFU not ready", "toIfu.req.ready = f1_ready && icacheReady → ifuPtr stalls"],
        ["Entry not f_to_send", "entry_is_to_send=false → toIfu.req.valid=false"],
        ["ifuFlush", "allowToIfu=false → IFU request blocked"],
        ["MMIO commit wait", "mmioCommitRead.valid + mmioLastCommit → IFU control"],
    ]
    add_table_from_rows(doc, ftq_flow_rows)

    doc.add_heading("5.8 Timing Hints", 2)
    ftq_timing = [
        ["Critical Path", "Description"],
        ["entry_is_to_send", "entry_fetch_status(ifuPtr.value) === f_to_send — indexed by ifuPtr"],
        ["validEntries", "distanceBetween(bpuPtr, commPtr) — directly gates io.fromBpu.resp.ready"],
        ["toIfuPcBundle mux", "3-way mux: bypass / last_cycle_to_ifu_fire / SRAM read"],
        ["commitStateQueueReg", "FtqSize × PredictWidth 2-bit registers — copyNum split reduces fanout"],
        ["FTBEntryGen", "Combinational: br slot insertion/movement, pftAddr calculation"],
    ]
    add_table_from_rows(doc, ftq_timing)

    doc.add_page_break()

    # ── 6. IBuffer Analysis ────────────────────────────────────────────────────
    doc.add_heading("6. IBuffer Analysis", 1)

    doc.add_heading("6.1 Module Summary", 2)
    doc.add_paragraph(
        "Role: Buffers fetch packets (up to PredictWidth instructions) received from IFU "
        "and supplies up to DecodeWidth CtrlFlow entries to Decode each cycle. "
        "Applies back-pressure when full; flushes entirely on backend redirect. "
        "Location: Frontend.scala → Module(new IBuffer) inside FrontendInlinedImp. "
        "Pipeline: 1 output register stage + optional bypass path."
    )

    doc.add_heading("6.2 Key Parameters", 2)
    ibuf_params = [
        ["Parameter", "Source", "Default", "Effect"],
        ["IBufSize", "XSCoreParamsKey.IBufSize", "48", "Total buffer entries (IBufNBank × bankSize)"],
        ["IBufNBank", "XSCoreParamsKey.IBufNBank", "6", "Bank count (≥ DecodeWidth required)"],
        ["PredictWidth", "HasXSParameter.PredictWidth", "16", "Max enqueue instructions per cycle"],
        ["DecodeWidth", "XSCoreParamsKey.DecodeWidth", "6", "Max dequeue instructions per cycle"],
        ["bankSize", "IBufSize / IBufNBank", "8", "Entries per bank"],
    ]
    add_table_from_rows(doc, ibuf_params)
    doc.add_paragraph("Constraints: IBufSize % IBufNBank == 0;  IBufNBank >= DecodeWidth")

    doc.add_heading("6.3 Interfaces", 2)
    ibuf_iface = [
        ["Port", "Dir", "Protocol", "Description"],
        ["io.in", "in", "Decoupled", "IFU → IBuffer (PredictWidth instructions)"],
        ["io.out(i)", "out", "Decoupled", "IBuffer → Decode (DecodeWidth entries)"],
        ["io.flush", "in", "Bool", "Backend redirect → full flush"],
        ["io.decodeCanAccept", "in", "Bool", "Decode can accept"],
        ["io.full", "out", "Bool", "IBuffer full (!allowEnq)"],
        ["io.ControlBTBMissBubble/TAGEMissBubble/...", "in", "Bool", "Flush reason for TopDown classification"],
        ["io.stallReason", "out", "StallReasonIO", "TopDown stall reason vector"],
    ]
    add_table_from_rows(doc, ibuf_iface)

    doc.add_heading("6.4 Internal Structure", 2)
    add_code_block(doc, IBUF_STRUCTURE)

    doc.add_heading("6.5 Enqueue Logic", 2)
    doc.add_paragraph(
        "numFromFetch = PopCount(io.in.bits.enqEnable). "
        "enqOffset(i) = PopCount(io.in.bits.valid.take(i)) — computes each instruction's write slot. "
        "Bypass: when enqPtr==deqPtr && decodeCanAccept, the first DecodeWidth instructions are "
        "forwarded directly to OutputEntries; only the remainder is written to ibuf registers. "
        "Full guard: allowEnq = (IBufSize - PredictWidth) >= numValidNext (PredictWidth margin)."
    )

    doc.add_heading("6.6 Dequeue Logic (2-Stage Read)", 2)
    doc.add_paragraph(
        "Stage 1: Each bank selects 1 entry via bankSize:1 Mux (deqInBankPtr index). "
        "Stage 2: Each output slot selects from IBufNBank results via IBufNBank:1 Mux (deqBankPtr index). "
        "DecodeWidth=6 reads each come from a different bank → no structural hazard."
    )

    doc.add_heading("6.7 Exception Handling", 2)
    exc_rows2 = [
        ["Exception info", "Storage", "Delivery"],
        ["Page Fault (PF)", "IBufferExceptionType.NonCrossPF / CrossPF", "cf.exceptionVec(instrPageFault)"],
        ["Guest Page Fault (GPF)", "NonCrossGPF / CrossGPF", "cf.exceptionVec(instrGuestPageFault)"],
        ["Access Fault (AF)", "NonCrossAF / CrossAF", "cf.exceptionVec(instrAccessFault)"],
        ["Illegal RVC", "rvcII", "cf.exceptionVec(EX_II)"],
        ["Cross-page IPF fix", "CrossPF", "cf.crossPageIPFFix"],
        ["Backend exception", "backendException: Bool", "cf.backendException passed through"],
    ]
    add_table_from_rows(doc, exc_rows2)
    doc.add_paragraph(
        "IBufferExceptionType encoding (3-bit): "
        "000=None, 001=NonCrossPF, 010=NonCrossGPF, 011=NonCrossAF, "
        "100=rvcII, 101=CrossPF, 110=CrossGPF, 111=CrossAF. "
        "bit[2]: isCrossPage, bit[1:0]: exception type."
    )

    doc.add_heading("6.8 Backpressure Chain", 2)
    add_code_block(doc, """
Back-pressure propagation:
  Backend not accept → decodeCanAccept=false
    → numOut=0 → outputEntries frozen
    → numValidNext increases → allowEnq=false
    → io.in.ready=false → IFU stall → F3 stall
    → icacheStop=true → ICache stall
    → FTQ toIfu.req.ready=false → further IFU requests blocked
""")

    doc.add_heading("6.9 Timing Hints", 2)
    ibuf_timing = [
        ["Critical Path", "Description"],
        ["Enqueue write mux", "IBufSize × PredictWidth Mux1H — select 1 source per ibuf entry"],
        ["Dequeue 2-stage read", "Stage1: bankSize:1 Mux × IBufNBank; Stage2: IBufNBank:1 Mux × DecodeWidth"],
        ["enqOffset PopCount", "PopCount(valid.take(i)) × PredictWidth — parallel prefix sum"],
        ["outputEntriesValidNum", "PriorityMuxDefault over DecodeWidth outputs — priority encoder"],
        ["numValidNext → allowEnq", "Addition + comparison — on io.in.ready critical path"],
        ["deqBankPtr update", "deqBankPtrVec(i) + numDeq × DecodeWidth — parallel circular ptr"],
    ]
    add_table_from_rows(doc, ibuf_timing)

    doc.add_page_break()

    # ── 7. Open Questions ─────────────────────────────────────────────────────
    doc.add_heading("7. Open Questions / TODO", 1)
    qs = [
        "MMIO fetch latency: How frequently does the ROB commit wait occur in practice?",
        "numDup=4 register replication: Quantify the timing improvement in post-route results.",
        "f2_mmio_mismatch_exception: Is the cacheable/non-cacheable boundary crossing case fully handled?",
        "PTWFilter ifilterSize: How does it affect iTLB miss frequency?",
        "IBuffer bypass path: How often is bypass actually used in waveform simulations?",
        "uBTB aliasing: PC[VAddrBits-1:23] is ignored in tag comparison — what is the alias rate for large address spaces?",
        "uBTB slot2: Always valid=false currently (TODO: 2-taken support).",
        "FTQ TODO (Ftq.scala): 'wait for IFU/ICache to remove bpu s2 flush' — S2 flush path not yet fully implemented.",
    ]
    for q in qs:
        p = doc.add_paragraph(style='List Bullet')
        p.add_run(q)

    return doc


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    out_path = '/Users/inkeuncho/Work/CPU_XS_02172026/src/main/scala/xiangshan/frontend/doc/frontend_combined.docx'
    doc = build_doc()
    doc.save(out_path)
    print(f"Saved: {out_path}")

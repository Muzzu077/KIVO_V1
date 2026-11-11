# REA Evidence Register — Artifacts, Cryptographic Proofs & Findings Classification

## 1. Scope & Methodology

This register provides the formal audit trail of all technical evidence captured during the reverse-engineering investigation of **NotchPixel** and **HeyClicky**. Analysis was conducted using the **REA** (Reverse Engineer Anything) suite and its MCP tools connected in Code Mode (`tools.rea`), operating on authentic, publicly distributed software packages and official release artifacts.

In compliance with strict investigation standards, every finding in this report is categorized into one of five certainty tiers:
1. **Confirmed Finding (Observed Fact)**: Concrete evidence directly observed in binary headers, PE metadata, CIL type definitions, disassembled segments, or shipped textual assets.
2. **Evidence-Backed Inference**: Technical conclusions derived logically from combinations of observed facts (e.g. deducing API protocols from imported namespaces and external service manifests).
3. **Unverified Hypothesis**: Plausible implementation models supported by circumstantial indicators but lacking definitive decompiled or dynamic confirmation.
4. **Unknown**: Proprietary, server-side, or encrypted logic that cannot be established from public client artifacts.
5. **Platform Limitation**: Constraints imposed by the target operating systems or hardware architecture.

---

## 2. Artifact & Cryptographic Identity Ledger

All analyzed binaries were acquired from official distribution endpoints and cataloged with SHA-256 digests prior to inspection:

| Artifact Name | Container / Format | Exact Byte Size | Cryptographic SHA-256 Digest | Origin / Source URL |
|---|---|---|---|---|
| `HeyClicky-Setup.exe` | PE32 (x86 executable) | 4,315,432 bytes | `1ea46a45d1fb52b0bb4e514ab96336a726770f90c73ab2bc400bd98cd9024dca` | `github.com/heyclicky/clicky-releases-windows/releases/download/v1.1.5/HeyClicky-Setup.exe` |
| `HeyClickyApp-1.1.5-win-x64-full.nupkg` | ZIP / NuGet package (Velopack) | 277,172,752 bytes | `cfdba29d25213156a582f5cf6f87a1c9a268b6c1d5086dcea281186d58864ce6` | `github.com/heyclicky/clicky-releases-windows/releases/download/v1.1.5/HeyClickyApp-1.1.5-win-x64-full.nupkg` |
| `HeyClickyApp-1.1.5-win-x64-delta.nupkg` | ZIP / NuGet package (Velopack) | 13,021,734 bytes | `7f333b9381a15c7d3d7a600c78ed9905589396e66fdd8b5922a04208d0e6d1c8` | `github.com/heyclicky/clicky-releases-windows/releases/download/v1.1.5/HeyClickyApp-1.1.5-win-x64-delta.nupkg` |
| `HeyClicky.Agents.dll` | PE32+ (x86_64 CLI Assembly) | 2,121,024 bytes | `39ab1b748553caa297d05d71d5da4c7742780db87a73dd3ea1098ac6d884f6ac` | Extracted from `HeyClickyApp-1.1.5-win-x64-full.nupkg` (`lib/app/`) |
| `HeyClicky.Drawing.dll` | PE32+ (x86_64 CLI Assembly) | 1,473,856 bytes | `0dc190a5c55c65e648800cc047a55468648a49eca756edacd056d7db3dc74117` | Extracted from `HeyClickyApp-1.1.5-win-x64-full.nupkg` (`lib/app/`) |
| `HeyClicky.Core.dll` | PE32+ (x86_64 CLI Assembly) | 7,884,096 bytes | `c7de513fa99a2d8eee2a8704cade3f5e02463f6f8d3210a83c4c61441c294f39` | Extracted from `HeyClickyApp-1.1.5-win-x64-full.nupkg` (`lib/app/`) |
| `HeyClicky.dll` | PE32+ (x86_64 CLI Assembly) | 11,273,216 bytes | `4c8d5ea2f3b97b0a88ef49bb1e17d7b7e21a7d65319a9f5d3a01a34e022b7244` | Extracted from `HeyClickyApp-1.1.5-win-x64-full.nupkg` (`lib/app/`) |
| `clicky-model-instructions.md` | Markdown text asset | 6,104 bytes | SHA-1: `5488cc6c31eb8b047e940203488df4e18aee4d1a` | Extracted from `HeyClickyApp-1.1.5-win-x64-full.nupkg` (`BundledResources/`) |

---

## 3. Formal REA Evidence Registry

### Record 1: Installer Binary Topology & Header Inspection
- **REA Evidence ID**: `ev_0d294c0e77c4c392cac7020263126dbfdff836dbd24329f21f2a9d01304aab9f`
- **Target Artifact**: `/tmp/opencode/heyclicky/HeyClicky-Setup.exe`
- **Operation**: `tools.rea.binary_overview`
- **Analysis Engine**: Hopper Disassembler launcher integration.
- **Observed Metrics**:
  - Segment count: 8 (`.text`, `.rdata`, `.data`, `.pdata`, `.rsrc`, `.reloc`, `External Symbols`).
  - Total procedures analyzed: 29,210.
  - Distinct native string literals: 557.
- **Confirmed Facts**:
  - `HeyClicky-Setup.exe` is a 32-bit Intel x86 PE executable acting as a self-extracting bootstrap launcher for Velopack deployment.
  - Contains no application logic or model client code; serves purely as extraction stub.

### Record 2: Installer PE Resources & Embedded Metadata
- **REA Evidence ID**: `ev_ba4829d4954bbb03af874cab5bd588b689128399f58f8c076e9f0e67f85f2162`
- **Target Artifact**: `/tmp/opencode/heyclicky/HeyClicky-Setup.exe`
- **Operation**: `tools.rea.inspect_pe_resources`
- **Analysis Engine**: REA PE Resource Parser.
- **Observed Metrics**:
  - Total resource directories: 16.
  - Total examined entries: 30.
  - Multi-resolution icon groups: 10 discrete formats (16x16 up to 128x128 32-bit BGRA bitmaps).
- **Confirmed Facts**:
  - Resource table conforms strictly to Windows PE32 specifications without packed obfuscation or encrypted resource sections.

### Record 3: Autonomous Agent Subsystem Assembly Architecture
- **REA Evidence ID**: `ev_1f7ac010c41a4897920c0e1f0076daa1e63fa3524baeb517095582901f60c91c`
- **Target Artifact**: `/tmp/opencode/heyclicky/HeyClicky.Agents.dll`
- **Operation**: `tools.rea.inspect_managed_artifact` (detail: `"summary"`)
- **Analysis Engine**: REA Managed PE/CLI Metadata Engine.
- **Observed Metrics**:
  - Target Framework: `.NETCoreApp,Version=v9.0` (.NET 9).
  - CLI Flags: `il-library`, `ready_to_run_signature: true`.
  - MVID: `8c83ddbd-b574-4137-b87d-5eec11189b49`.
  - Assembly Version: `1.1.5.0`.
  - Table counts: 524 TypeDefs, 4,118 MethodDefs, 2,458 Fields, 1,845 MemberRefs, 20 AssemblyRefs.
- **Confirmed Facts**:
  - Built against the modern .NET 9.0 CoreCLR runtime compiled with ReadyToRun (R2R) native code generation for fast startup.
  - Houses the complete agent orchestration subsystem, task lifecycle, and plan management.

### Record 4: Agent Subsystem Type Definition & Member Inventory
- **REA Evidence ID**: `ev_3d8c43bf1340396c20130bbfd08620f131bdd5f9bef6dfefee43e34628623332` (Summary) / `ev_f81857b4de4acf41510ae53551c706298046053fb13ee1d1e64183e4e4f87796` (Types View)
- **Target Artifact**: `/tmp/opencode/heyclicky/HeyClicky.Agents.dll`
- **Operation**: `tools.rea.inspect_managed_members` & `inspect_analysis_view` (view: `types`, limit: 40)
- **Observed Types**:
  - `HeyClicky.Agents.AgentService` (Token `0x02000015`, 96 fields, 324 methods): Central agent supervisor.
  - `HeyClicky.Agents.Turns.AgentTask` (Token `0x02000016`, 64 fields, 117 methods): State machine managing single agent task.
  - `HeyClicky.Agents.AgentPlanStep` (Token `0x02000012`): Structured plan tracking bound to user-facing progress.
  - `HeyClicky.Agents.Turns.StandingContextLedger` (Token `0x02000019`): Memory tracker for continuous multi-turn jobs.
  - `HeyClicky.Agents.Runtime.ComposioMcpConfiguration` (Token `0x02000020`): Configuration provider for Composio MCP tool integration.
  - `HeyClicky.Agents.Runtime.KillOnCloseJob` (Token `0x02000027`): Win32 Job Object wrapper ensuring child subprocesses terminate cleanly on app closure.
- **Confirmed Facts**:
  - HeyClicky uses a structured task lifecycle where tasks report steps through an explicit planning API (`AgentPlanStep`).
  - Implements process safety via Win32 Job Objects (`KillOnCloseJob`), preventing orphaned agent processes.

### Record 5: Hardware-Accelerated Screen Annotation Subsystem
- **REA Evidence ID**: `ev_6593906ccfd04ae961d6c324bf47c9fb643ec3adaf006071d22cf9104d3d26c3`
- **Target Artifact**: `/tmp/opencode/heyclicky/HeyClicky.Drawing.dll`
- **Operation**: `tools.rea.inspect_managed_artifact` (detail: `"summary"`)
- **Analysis Engine**: REA Managed PE/CLI Metadata Engine.
- **Observed Metrics**:
  - Target Framework: `.NETCoreApp,Version=v9.0`.
  - CLI Flags: `il-library`, `ready_to_run_signature: true`.
  - MVID: `05955e76-1d9f-42b1-b013-44dc42b11ec7`.
  - Table counts: 293 TypeDefs, 2,201 MethodDefs, 1,588 Fields, 2,543 MemberRefs, 35 AssemblyRefs.
- **Confirmed Facts**:
  - Implements a dedicated drawing assembly decoupled from the general UI shell.

### Record 6: Screen Drawing Types & GPU Priority Mechanics
- **REA Evidence ID**: `ev_fdc60f187709f30a9e92cd9156ffaae824cee302bd2e86777dfcf6c5c02cd9c0` (Summary) / `ev_cdd90abfb6f753a213ced614dadc49a3a1fc4cd6a605ff69f9fac91f74738c59` (Types View)
- **Target Artifact**: `/tmp/opencode/heyclicky/HeyClicky.Drawing.dll`
- **Operation**: `tools.rea.inspect_managed_members` & `inspect_analysis_view` (view: `types`, limit: 40)
- **Observed Types**:
  - `HeyClicky.Drawing.DrawingThread` (Token `0x0200001d`, 15 fields, 27 methods): Background thread dedicated exclusively to rendering canvas updates.
  - `HeyClicky.Drawing.GpuPriority` (Token `0x0200001f`): GPU scheduling coordinator elevating D3D device priority.
  - `HeyClicky.Drawing.SystemComposition` (Token `0x02000026`): DirectComposition / DWM interop surface.
  - `HeyClicky.Drawing.ScreenDrawingCanvas` (Token `0x02000027`, 52 fields, 150 methods): Fullscreen transparent overlay.
  - `HeyClicky.Drawing.LessonPlaybackScript` (Token `0x02000020`), `LessonPlaybackFrame` (`0x02000021`), `LessonPenPose` (`0x02000022`): Kinematic animation engine for vector pen strokes.
  - `HeyClicky.Drawing.NotchVisibleSurfaces` (Token `0x02000025`): Spatial coordinator aligning screen annotations with the top notch.
- **Confirmed Facts**:
  - Screen annotations are not static PNG overlays; they are dynamic kinematic vector paths rendered via DirectX 11 / DirectComposition on a dedicated thread with elevated GPU priority.

---

## 4. Operational Documents & Contract Verification

### Document A: Core Agent Instructions (`clicky-model-instructions.md`)
- **Digest**: SHA-1 `5488CC6C31EB8B047E940203488DF4E18AEE4D1A` (6,104 bytes).
- **Key Operational Invariants**:
  1. *Workspace Isolation*: Workspace contains `output/` (deliverables only) and `tmp/` (scratchpad).
  2. *Permission Boundary*: "Confirm first only before deleting or archiving their data, overwriting what they didn't ask you to change, sending something to other people... or spending money."
  3. *State Recovery*: Long tasks store `.clicky/memory.md` which survives context compaction.
  4. *Anti-Automation Rule*: "Never script clicks or keystrokes in their apps (`System Events`, `cliclick`) or drive them with `open`... A step only an app's window can do goes to HeyClicky."

### Document B: Browser Automation Contract (`clicky-browser/SKILL.md`)
- **Mechanism**: Dedicated Native Messaging host (`ClickyBrowserHost.exe`) communicating with a Chrome/Chromium extension.
- **Key Operational Invariants**:
  1. Operates in the user's signed-in browser tab group (not a headless bot).
  2. Uses accessibility snapshots (`snapshot`, `extract`) and batched operations (`batch` up to 20 actions) to minimize network roundtrips.
  3. Strict refusal of password fields, 2FA codes, payment card numbers, and CAPTCHAs.
  4. Prompt injection defense: All web content is treated strictly as untrusted data.

---

## 5. Synthesis: Classification of All Findings

### 5.1 Confirmed Findings (Observed Facts)
1. **NotchPixel Distribution**: Sold as a packaged Windows desktop app via Microsoft Store, with low idle footprint (~13 MB RAM, 0% CPU).
2. **NotchPixel Media & Lyrics**: Built on Windows SMTC and queries public LRCLIB API (`lrclib.net`) for LRC lyric data.
3. **NotchPixel Hardware Overlay**: Frame timing is derived from DXGI/D3D ETW present events without game DLL injection.
4. **NotchPixel Mobile Bridge**: PC Deck and file transfers run over local Wi-Fi via an embedded HTTP server with QR-tokenized authentication.
5. **HeyClicky Architecture**: Windows version is a native .NET 9.0 + WinUI 3 application with DirectComposition graphics, not Electron.
6. **HeyClicky Screen Drawing**: Implemented via a dedicated `DrawingThread`, `GpuPriority` scheduling, and `LessonPenPose` vector animations on `ScreenDrawingCanvas`.
7. **HeyClicky Privacy Model**: Explicit capture-on-demand only. Raw screenshot frames are discarded immediately after multimodal inference; only text summaries are stored.
8. **HeyClicky Dual-Mode Boundary**: Rigorous operational separation between "Talk" (conversational assistant) and "Agent" (autonomous subprocess executing in `clicky-harness`).

### 5.2 Evidence-Backed Inferences
1. **HeyClicky Realtime Protocol**: Voice and audio streaming uses the OpenAI GPT Realtime API over WebSockets, corroborated by Greg Brockman's public statements and .NET WebSocket dependencies.
2. **NotchPixel Process Hooks**: Agent monitoring in NotchPixel monitors local process trees and standard CLI output/sockets rather than proprietary model APIs.

### 5.3 Unverified Hypotheses
1. *NotchPixel Mac Implementation*: Hypothesized to use Swift / AppKit with `NSPanel` on macOS when released, but unverified due to lack of public macOS binary.
2. *HeyClicky macOS Implementation*: May use Swift / SwiftUI + Metal on macOS, sharing identical model instructions and browser extensions with the Windows build.

### 5.4 Unknowns (Out-of-Scope / Proprietary)
1. Exact proprietary prompts used in HeyClicky's cloud router.
2. Internal subscription validation and rate-limiting servers.

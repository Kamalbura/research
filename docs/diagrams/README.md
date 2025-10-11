# Architecture diagrams – how to view

There are two easy ways to view these Mermaid diagrams:

- In VS Code
    - Open any .md file here and press Ctrl+Shift+V (Open Preview) or use the split preview.
    - VS Code’s built-in Markdown preview renders Mermaid. If not, install the “Markdown Preview Mermaid Support” extension.
- In a browser
    - Open index.html in your browser. It uses the Mermaid CDN to render all diagrams on one page.

Files in this folder:
- system_overview.md – end-to-end drone↔GCS↔security plane
- core_modules.md – how core/* modules wire together
- data_plane.md – AEAD framing, header layout, nonce and ports
- handshake.md – KEM+SIG handshake and HKDF flow
- rekey_fsm.md – control-plane 2PC state machine
- scheduler_and_follower.md – orchestration on both sides
- index.html – one-page viewer with all diagrams

Tip: GitHub also renders Mermaid in Markdown directly if you browse the repo online.
# Diagrams Index

This directory contains all visual documentation for the Post-Quantum Cryptographic Framework, organized by category with individual files for each diagram type.

## 📁 Directory Structure

```
docs/diagrams/
├── README.md                    # This index file
├── system/                      # System architecture diagrams
│   ├── overview.md             # Complete system overview
│   ├── modules.md              # Core module relationships
│   └── data-flow.md            # Data flow visualization
├── protocols/                   # Protocol flow diagrams
│   ├── handshake.md            # TCP handshake sequence
│   ├── data-transport.md       # UDP data plane flow
│   └── runtime-switching.md    # Algorithm switching flow
├── algorithms/                  # Cryptographic algorithm diagrams
│   ├── algorithm-matrix.md     # 21-suite combinations
│   ├── ml-kem.md              # ML-KEM key exchange
│   ├── signatures.md          # Digital signature comparison
│   └── security-levels.md     # NIST security level mapping
├── performance/                 # Performance and analysis diagrams
│   ├── benchmarks.md          # Performance comparison charts
│   ├── timeline.md            # Quantum threat timeline
│   └── testing.md             # Test coverage visualization
└── implementation/             # Implementation-specific diagrams
    ├── state-machines.md       # FSM for runtime switching
    ├── packet-format.md        # Wire format specifications
    └── deployment.md           # Deployment architecture
```

## 🎯 Quick Access by Use Case

### For Academic Papers
- [Quantum Threat Timeline](performance/timeline.md) - Publication-ready threat analysis
- [Algorithm Comparison](algorithms/algorithm-matrix.md) - NIST algorithm overview
- [System Overview](system/overview.md) - High-level architecture for papers

### For Technical Documentation
- [Complete System Architecture](system/overview.md) - Detailed module interactions
- [Protocol Flows](protocols/handshake.md) - Step-by-step protocol execution
- [Implementation Details](implementation/state-machines.md) - Developer-focused diagrams

### For Presentations
- [Performance Benchmarks](performance/benchmarks.md) - Results visualization
- [Security Analysis](algorithms/security-levels.md) - Security property diagrams
- [Deployment Guide](implementation/deployment.md) - Operational diagrams

## 📊 Diagram Types

### Mermaid Diagrams
All diagrams use Mermaid syntax for consistency and maintainability:
- **Flowcharts**: System architecture and data flow
- **Sequence Diagrams**: Protocol interactions and handshakes
- **State Diagrams**: Runtime state machines and transitions
- **Gantt Charts**: Timeline and threat analysis
- **Class Diagrams**: Module relationships and dependencies

### Styling Standards
- **Academic**: Clean, professional styling suitable for research papers
- **Technical**: Detailed, implementation-focused with specific function names
- **Presentation**: High contrast, clear labeling for slides and demos

## 🔄 Navigation Between Diagrams

Each diagram file includes:
- **Context**: How the diagram fits in the overall system
- **Prerequisites**: What to understand before viewing this diagram
- **Related Diagrams**: Links to complementary visualizations
- **Implementation Links**: References to actual code files

## 📝 Usage Guidelines

### For Research Papers
Use simplified versions focusing on novel contributions:
```markdown
![System Overview](docs/diagrams/system/overview.md#simplified)
```

### For Technical Documentation
Use detailed versions with implementation specifics:
```markdown
![Complete Architecture](docs/diagrams/system/overview.md#detailed)
```

### For Developer Guides
Use implementation-focused versions with code references:
```markdown
![Module Interactions](docs/diagrams/system/modules.md#implementation)
```

---

**Return to**: [Technical Documentation](../technical/README.md) | [Research Paper Section 4](../../SECTION_4_CRYPTOGRAPHIC_FRAMEWORK.md)
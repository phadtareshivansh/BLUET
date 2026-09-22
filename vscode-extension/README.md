# BLUET VS Code Extension

VS Code extension for the BLUET agentic refactoring toolbox. Provides LSP integration, status bar parity monitoring, and side-by-side diff views.

## Features

- **LSP Integration**: Connects to the BLUET language server (`bluet lsp`) for real-time refactoring
- **Status Bar**: Live parity status updates during refactoring jobs
- **Code Actions**: "Run BLUET Refactor" code action on Python/Java files
- **Diff View**: Side-by-side comparison of legacy vs. refactored code using VS Code's native diff viewer
- **Commands**: 
  - `BLUET: Run Refactor` (Ctrl+Alt+R / Cmd+Alt+R)
  - `BLUET: Show Diff`

## Installation

### From Source

```bash
cd vscode-extension
npm install
npm run compile
```

Then press `F5` in VS Code to launch the Extension Development Host.

### Packaging

```bash
cd vscode-extension
vsce package
```

## Configuration

| Setting | Description | Default |
|---------|-------------|---------|
| `bluet.serverPath` | Path to bluet CLI executable | `bluet` |
| `bluet.workspaceRoot` | Workspace root for language server | `${workspaceFolder}` |
| `bluet.targetLanguage` | Target language for refactoring | Auto-detect from file extension |

## Usage

1. Open a Python (`.py`) or Java (`.java`) file
2. Press `Ctrl+Alt+R` (or `Cmd+Alt+R` on Mac) to trigger a refactor
3. Watch the status bar for live parity updates:
   - 🔄 Analyzing / Indexing / Refactoring / Verifying
   - ✅ Parity OK
   - ❌ Parity Failed (N regressions) - click to view diff
4. When complete, use `BLUET: Show Diff` to see side-by-side comparison

## Requirements

- VS Code 1.80+
- BLUET CLI installed and in PATH (or configured via `bluet.serverPath`)
- Python 3.11+ environment with BLUET installed

## Development

### Project Structure

```
vscode-extension/
├── src/
│   ├── extension.ts       # Main entry point
│   ├── statusBar.ts       # Status bar manager
│   ├── diffProvider.ts    # Diff view provider
│   └── *.test.ts          # Tests
├── .vscode/
│   └── launch.json        # Debug configurations
├── package.json           # Extension manifest
├── tsconfig.json          # TypeScript config
└── jest.config.js         # Jest config
```

### Running Tests

```bash
npm test
```

### Linting

```bash
npm run lint
```

## Architecture

The extension uses the VS Code Language Client protocol to communicate with the BLUET LSP server over stdio. The server runs as a separate process (`bluet lsp`) and handles:

- `textDocument/codeAction` - Offers "Run BLUET Refactor" action
- `bluet/parityStatus` - Custom notification for live status updates
- `textDocument/publishDiagnostics` - Inline diagnostics for parity failures

The extension registers a custom content provider (`bluet-diff:`) for diff content and uses `vscode.diff` command for the native diff viewer.

## License

MIT
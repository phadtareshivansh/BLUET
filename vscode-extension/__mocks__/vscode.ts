export const StatusBarAlignment = {
    Left: 1,
    Center: 2,
    Right: 3,
};

export class StatusBarItem {
    text = '';
    tooltip = '';
    command = '';
    backgroundColor?: { id: string };
    
    show() {}
    hide() {}
    dispose() {}
}

export class ThemeColor {
    constructor(public id: string) {}
}

export const window = {
    createStatusBarItem: (alignment?: number, priority?: number) => {
        return new StatusBarItem();
    },
};

export const Disposable = {
    from: (...disposables: { dispose: () => void }[]) => {
        return {
            dispose: () => disposables.forEach(d => d.dispose()),
        };
    },
};

export const commands = {
    registerCommand: (command: string, callback: (...args: unknown[]) => void) => {
        return { dispose: () => {} };
    },
    executeCommand: async (command: string, ...args: unknown[]) => {
        return undefined;
    },
};

export const workspace = {
    getConfiguration: (section?: string) => {
        return {
            get: (key: string, defaultValue?: unknown) => defaultValue,
            update: async () => {},
        };
    },
    workspaceFolders: [],
    onDidChangeConfiguration: (listener: (e: { affectsConfiguration: (section: string) => boolean }) => void) => {
        return { dispose: () => {} };
    },
};

export const Uri = {
    file: (path: string) => ({ fsPath: path, scheme: 'file', path }),
    parse: (str: string) => ({ fsPath: str, scheme: 'file', path: str }),
};

export const Range = class Range {
    constructor(
        public start: Position,
        public end: Position
    ) {}
};

export class Position {
    constructor(
        public line: number,
        public character: number
    ) {}
}

export const workspaceFs = {
    readFile: async (uri: { fsPath: string }) => new Uint8Array(),
    writeFile: async (uri: { fsPath: string }, content: Uint8Array) => {},
};

export const TextDocumentContentProvider = class TextDocumentContentProvider {
    provideTextDocumentContent(uri: { fsPath: string }): string | Thenable<string> {
        return '';
    }
};
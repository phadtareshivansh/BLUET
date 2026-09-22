/*---------------------------------------------------------------------------------------------
 *  Copyright (c) BLUET. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import * as vscode from 'vscode';
import { LanguageClient, LanguageClientOptions, ServerOptions, TransportKind } from 'vscode-languageclient/node';
import { StatusBarManager } from './statusBar';
import { DiffProvider } from './diffProvider';

let client: LanguageClient;
let statusBarManager: StatusBarManager;
let diffProvider: DiffProvider;

export function activate(context: vscode.ExtensionContext) {
    console.log('BLUET extension is activating...');

    // Load configuration
    const config = vscode.workspace.getConfiguration('bluet');
    const serverPath = config.get<string>('serverPath', 'bluet');
    const workspaceRoot = config.get<string>('workspaceRoot', '${workspaceFolder}');
    const targetLanguage = config.get<string>('targetLanguage', '');

    // Resolve workspace root
    const resolvedWorkspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();

    // Server options - run bluet lsp over stdio
    const serverOptions: ServerOptions = {
        command: serverPath,
        args: ['lsp', resolvedWorkspaceRoot],
        transport: TransportKind.stdio,
        options: {
            cwd: resolvedWorkspaceRoot,
            env: {
                ...process.env,
                BLUET_TARGET_LANGUAGE: targetLanguage,
            }
        }
    };

    // Client options
    const clientOptions: LanguageClientOptions = {
        documentSelector: [
            { scheme: 'file', language: 'python' },
            { scheme: 'file', language: 'java' },
        ],
        synchronize: {
            fileEvents: vscode.workspace.createFileSystemWatcher('**/*.{py,java}')
        },
        initializationOptions: {
            targetLanguage: targetLanguage
        },
        middleware: {
            // Handle custom notifications from the server
            workspace: {
                didChangeConfiguration: async (params) => {
                    // Handle configuration changes if needed
                }
            }
        }
    };

    // Create the language client
    client = new LanguageClient(
        'bluet',
        'BLUET Language Server',
        serverOptions,
        clientOptions
    );

    // Initialize status bar manager
    statusBarManager = new StatusBarManager(client);
    context.subscriptions.push(statusBarManager);

    // Initialize diff provider
    diffProvider = new DiffProvider(client, context);
    context.subscriptions.push(diffProvider);

    // Register commands
    const runRefactorCommand = vscode.commands.registerCommand('bluet.runRefactor', async () => {
        await runRefactor();
    });
    context.subscriptions.push(runRefactorCommand);

    const showDiffCommand = vscode.commands.registerCommand('bluet.showDiff', async (jobId?: string) => {
        await diffProvider.showDiff(jobId);
    });
    context.subscriptions.push(showDiffCommand);

    // Start the client
    client.start().then(() => {
        console.log('BLUET language server started');
        statusBarManager.updateStatus('idle', 'BLUET ready');
    }).catch((err) => {
        console.error('Failed to start BLUET language server:', err);
        vscode.window.showErrorMessage(`Failed to start BLUET language server: ${err.message}`);
        statusBarManager.updateStatus('error', 'BLUET failed to start');
    });

    // Listen for custom notifications from the server
    client.onNotification('bluet/parityStatus', (params: any) => {
        handleParityStatus(params);
    });

    context.subscriptions.push(
        vscode.Disposable.from(
            new vscode.Disposable(() => {
                client.stop();
            })
        )
    );
}

async function runRefactor(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('No active editor');
        return;
    }

    const document = editor.document;
    if (document.languageId !== 'python' && document.languageId !== 'java') {
        vscode.window.showWarningMessage('BLUET refactor only supports Python and Java files');
        return;
    }

    // Trigger code action - this will show the "Run BLUET Refactor" code action
    const actions = await vscode.commands.executeCommand<vscode.CodeAction[]>(
        'vscode.executeCodeActionProvider',
        document.uri,
        new vscode.Range(0, 0, document.lineCount, 0),
        { diagnostics: [], only: ['refactor'] }
    );

    const bluetAction = actions?.find(a => a.title === 'Run BLUET Refactor');
    if (bluetAction && bluetAction.command) {
        await vscode.commands.executeCommand(bluetAction.command.command, ...(bluetAction.command.arguments || []));
        statusBarManager.updateStatus('analyzing', 'BLUET: Analyzing...');
    } else {
        vscode.window.showWarningMessage('BLUET refactor action not available for this file');
    }
}

function handleParityStatus(params: any): void {
    const { status, stage, jobId, counterExamples } = params;
    
    console.log(`BLUET parity status: ${status} (${stage})`);
    
    switch (status) {
        case 'pending':
        case 'running':
            statusBarManager.updateStatus('analyzing', `BLUET: ${stage}...`);
            break;
        case 'pass':
            statusBarManager.updateStatus('pass', 'BLUET: Parity OK');
            vscode.window.showInformationMessage('BLUET: Refactoring completed successfully - parity check passed!');
            break;
        case 'fail':
            statusBarManager.updateStatus('fail', `BLUET: Parity failed (${counterExamples?.length || 0} regressions)`);
            vscode.window.showErrorMessage(
                `BLUET: Parity check failed with ${counterExamples?.length || 0} regression(s)`,
                'Show Diff'
            ).then(selection => {
                if (selection === 'Show Diff' && jobId) {
                    vscode.commands.executeCommand('bluet.showDiff', jobId);
                }
            });
            break;
        case 'skip':
            statusBarManager.updateStatus('skip', 'BLUET: Verification skipped');
            vscode.window.showInformationMessage('BLUET: Verification skipped (no checkable functions)');
            break;
        case 'error':
            statusBarManager.updateStatus('error', 'BLUET: Error during refactoring');
            vscode.window.showErrorMessage('BLUET: An error occurred during refactoring');
            break;
        default:
            statusBarManager.updateStatus('unknown', `BLUET: ${stage}`);
    }
}

export function deactivate(): Thenable<void> | undefined {
    console.log('BLUET extension deactivating...');
    if (client) {
        return client.stop();
    }
    return undefined;
}
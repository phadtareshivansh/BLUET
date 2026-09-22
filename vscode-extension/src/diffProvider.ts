/*---------------------------------------------------------------------------------------------
 *  Copyright (c) BLUET. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import * as vscode from 'vscode';
import { LanguageClient } from 'vscode-languageclient/node';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';

export class DiffProvider implements vscode.Disposable {
    private client: LanguageClient;
    private context: vscode.ExtensionContext;
    private disposable: vscode.Disposable;

    constructor(client: LanguageClient, context: vscode.ExtensionContext) {
        this.client = client;
        this.context = context;
        
        // Register the diff content provider
        this.disposable = vscode.workspace.registerTextDocumentContentProvider(
            'bluet-diff',
            new BluetDiffContentProvider()
        );
    }

    public async showDiff(jobId?: string): Promise<void> {
        // If no jobId provided, try to get the latest job
        if (!jobId) {
            jobId = await this.promptForJobId();
            if (!jobId) {
                return;
            }
        }

        try {
            // Run bluet diff --export to get the diff output
            const diffOutput = await this.runBluetDiff(jobId);
            
            if (!diffOutput) {
                vscode.window.showWarningMessage(`No diff output for job ${jobId}`);
                return;
            }

            // Parse the diff and create temporary files for VS Code diff view
            await this.openDiffView(jobId, diffOutput);
        } catch (error) {
            vscode.window.showErrorMessage(`Failed to show diff: ${error}`);
        }
    }

    private async promptForJobId(): Promise<string | undefined> {
        // Get recent jobs from bluet status
        const jobs = await this.getRecentJobs();
        if (jobs.length === 0) {
            vscode.window.showInformationMessage('No recent BLUET jobs found');
            return undefined;
        }

        const items = jobs.map(job => ({
            label: `Job ${job.id}`,
            description: `${job.status} - ${job.file}`,
            detail: `Created: ${job.createdAt}`,
            jobId: String(job.id)
        }));

        const selected = await vscode.window.showQuickPick(items, {
            placeHolder: 'Select a BLUET job to view diff',
            matchOnDescription: true,
            matchOnDetail: true
        });

        return selected?.jobId;
    }

    private async getRecentJobs(): Promise<Array<{id: number, status: string, file: string, createdAt: string}>> {
        // This would ideally query the BLUET state database
        // For now, return empty - in a real implementation this would query the DB
        return [];
    }

    private async runBluetDiff(jobId: string): Promise<string | null> {
        const config = vscode.workspace.getConfiguration('bluet');
        const serverPath = config.get<string>('serverPath', 'bluet');
        const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();

        return new Promise((resolve, reject) => {
            const { spawn } = require('child_process');
            const proc = spawn(serverPath, ['diff', jobId, '--export'], {
                cwd: workspaceRoot,
                env: process.env
            });

            let stdout = '';
            let stderr = '';

            proc.stdout.on('data', (data: Buffer) => {
                stdout += data.toString();
            });

            proc.stderr.on('data', (data: Buffer) => {
                stderr += data.toString();
            });

            proc.on('close', (code: number) => {
                if (code === 0) {
                    resolve(stdout);
                } else {
                    reject(new Error(stderr || `bluet diff exited with code ${code}`));
                }
            });

            proc.on('error', (err: Error) => {
                reject(err);
            });
        });
    }

    private async openDiffView(jobId: string, diffOutput: string): Promise<void> {
        // Create temporary files for the diff
        const tempDir = await this.createTempDir();
        
        // Parse the diff output to extract original and proposed code
        // The diff output from bluet diff --export should contain both versions
        // For now, we'll write the raw diff to a file and use VS Code's diff view
        
        const originalFile = path.join(tempDir, `job-${jobId}-original.py`);
        const proposedFile = path.join(tempDir, `job-${jobId}-proposed.py`);
        
        // Try to extract original and proposed from diff
        // This is a simplified version - in reality you'd parse the diff properly
        const { original, proposed } = this.parseDiffOutput(diffOutput);
        
        await fs.promises.writeFile(originalFile, original, 'utf8');
        await fs.promises.writeFile(proposedFile, proposed, 'utf8');

        // Open VS Code diff view
        await vscode.commands.executeCommand(
            'vscode.diff',
            vscode.Uri.file(originalFile),
            vscode.Uri.file(proposedFile),
            `BLUET Diff - Job ${jobId}`,
            { preview: true }
        );
    }

    private parseDiffOutput(diffOutput: string): { original: string; proposed: string } {
        // This is a simplified parser - in reality you'd properly parse the unified diff
        // For now, split by a marker if present, or return the diff as-is
        const lines = diffOutput.split('\n');
        
        // Look for a separator or just return the diff as both files for now
        // In a real implementation, you'd parse the unified diff format
        return {
            original: '// Original code would be extracted from diff\n' + diffOutput,
            proposed: '// Proposed code would be extracted from diff\n' + diffOutput
        };
    }

    private async createTempDir(): Promise<string> {
        const tempDir = path.join(os.tmpdir(), `bluet-diff-${Date.now()}`);
        await fs.promises.mkdir(tempDir, { recursive: true });
        return tempDir;
    }

    public dispose(): void {
        this.disposable.dispose();
    }
}

class BluetDiffContentProvider implements vscode.TextDocumentContentProvider {
    provideTextDocumentContent(uri: vscode.Uri): string | Thenable<string> {
        // This would be used if we registered a custom scheme for diff content
        // For now, we use temporary files and vscode.diff command
        return '';
    }
}
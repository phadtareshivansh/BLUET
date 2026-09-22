/*---------------------------------------------------------------------------------------------
 *  Copyright (c) BLUET. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import * as vscode from 'vscode';
import { LanguageClient } from 'vscode-languageclient/node';

export class StatusBarManager implements vscode.Disposable {
    private statusBarItem: vscode.StatusBarItem;
    private client: LanguageClient;
    private currentStatus: string = 'idle';

    constructor(client: LanguageClient) {
        this.client = client;
        
        // Create status bar item
        this.statusBarItem = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Right,
            100 // priority
        );
        
        this.statusBarItem.command = 'bluet.showDiff';
        this.statusBarItem.tooltip = 'Click to show BLUET diff';
        this.updateStatus('idle', 'BLUET ready');
        this.statusBarItem.show();
    }

    public updateStatus(status: string, message: string): void {
        this.currentStatus = status;
        
        // Set icon and color based on status
        let icon = '$(sync~spin)';
        let color: string | undefined;
        
        switch (status) {
            case 'idle':
                icon = '$(check)';
                break;
            case 'analyzing':
            case 'indexing':
            case 'refactoring':
            case 'verifying':
            case 'self-healing':
                icon = '$(sync~spin)';
                break;
            case 'pass':
                icon = '$(pass-filled)';
                color = 'lightgreen';
                break;
            case 'fail':
                icon = '$(error)';
                color = 'lightcoral';
                break;
            case 'skip':
                icon = '$(circle-slash)';
                color = 'yellow';
                break;
            case 'error':
                icon = '$(warning)';
                color = 'orange';
                break;
            default:
                icon = '$(question)';
        }
        
        this.statusBarItem.text = `${icon} ${message}`;
        if (color) {
            this.statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        }
    }

    public getStatus(): string {
        return this.currentStatus;
    }

    public dispose(): void {
        this.statusBarItem.dispose();
    }
}
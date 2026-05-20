// SPDX-License-Identifier: AGPL-3.0-or-later
import { Component, type ReactNode, type ErrorInfo } from 'react';

interface Props {
  children: ReactNode;
  name?: string;
}

interface State {
  hasError: boolean;
}

export class IslandErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(_: Error): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`[IslandErrorBoundary${this.props.name ? `:${this.props.name}` : ''}]`, error, info);
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div
          role="alert"
          className="my-8 rounded-lg border border-viindoo-warning/40 bg-viindoo-bg-2 p-6 text-center text-viindoo-on-dark-muted"
        >
          <p className="font-mono text-sm">
            Interactive demo unavailable. Refresh the page or visit{' '}
            <a href="https://odoo-semantic.viindoo.com" className="text-viindoo-primary hover:underline">
              odoo-semantic.viindoo.com
            </a>
            .
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}

export default IslandErrorBoundary;

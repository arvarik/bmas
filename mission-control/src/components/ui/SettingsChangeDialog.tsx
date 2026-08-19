"use client";

import { useEffect, useId, useRef } from "react";
import { AlertTriangle, ArrowRight } from "lucide-react";

export interface SettingsChange {
  label: string;
  before: string;
  after: string;
}

interface SettingsChangeDialogProps {
  open: boolean;
  title: string;
  description: string;
  changes: SettingsChange[];
  confirmLabel: string;
  busy?: boolean;
  danger?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function SettingsChangeDialog({
  open,
  title,
  description,
  changes,
  confirmLabel,
  busy = false,
  danger = false,
  onCancel,
  onConfirm,
}: SettingsChangeDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (open && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }

    if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      className="settings-change-dialog"
      role={danger ? "alertdialog" : "dialog"}
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
      onClose={() => {
        if (open && !busy) onCancel();
      }}
    >
      <div className="settings-change-dialog__header">
        {danger ? <AlertTriangle size={18} aria-hidden="true" /> : null}
        <div>
          <h2 id={titleId}>{title}</h2>
          <p id={descriptionId}>{description}</p>
        </div>
      </div>

      <div className="settings-change-dialog__changes" aria-label="Proposed changes">
        {changes.map((change) => (
          <div className="settings-change-dialog__change" key={change.label}>
            <span className="settings-change-dialog__label">{change.label}</span>
            <div className="settings-change-dialog__values">
              <code>{change.before}</code>
              <ArrowRight size={13} aria-hidden="true" />
              <code>{change.after}</code>
            </div>
          </div>
        ))}
      </div>

      <p className="settings-change-dialog__note">
        This change affects new tasks in this server session. A server restart restores the YAML
        configuration.
      </p>

      <div className="settings-change-dialog__actions">
        <button
          type="button"
          className="settings-btn settings-btn--ghost"
          onClick={onCancel}
          disabled={busy}
          autoFocus
        >
          Cancel
        </button>
        <button
          type="button"
          className={`settings-btn ${danger ? "settings-btn--danger" : "settings-btn--primary"}`}
          onClick={onConfirm}
          disabled={busy}
        >
          {busy ? <span className="settings-spinner" aria-hidden="true" /> : null}
          {busy ? "Applying…" : confirmLabel}
        </button>
      </div>
    </dialog>
  );
}

"use client";

/**
 * Settings controls — the small set of form primitives the Settings page
 * composes: a row, a toggle, a number field, a text field, and a segmented
 * control. Each control is a plain labelled input with app styling.
 */

import { useId, type ReactNode } from "react";
import { RotateCcw } from "lucide-react";

export function SettingsRow({
  label,
  description,
  control,
  htmlFor,
  overridden = false,
  changed = false,
  onReset,
  resetLabel = "Use bmas.yaml value",
  align = "center",
}: {
  label: ReactNode;
  description?: ReactNode;
  control: ReactNode;
  htmlFor?: string;
  /** Saved value differs from bmas.yaml. */
  overridden?: boolean;
  /** Draft value differs from the saved value. */
  changed?: boolean;
  onReset?: () => void;
  resetLabel?: string;
  align?: "center" | "start";
}) {
  const Label = htmlFor ? "label" : "div";
  return (
    <div className={`settings-row ${changed ? "settings-row--changed" : ""}`} data-align={align}>
      <div className="settings-row__text">
        <Label className="settings-row__label" {...(htmlFor ? { htmlFor } : {})}>
          {label}
          {overridden ? <span className="settings-pill settings-pill--session" title="This value is a session override. It resets when the daemon restarts.">Session</span> : null}
          {changed ? <span className="settings-pill settings-pill--changed">Unsaved</span> : null}
        </Label>
        {description ? <p className="settings-row__description">{description}</p> : null}
        {onReset && overridden ? (
          <button type="button" className="settings-row__reset" onClick={onReset}>
            <RotateCcw size={12} aria-hidden="true" /> {resetLabel}
          </button>
        ) : null}
      </div>
      <div className="settings-row__control">{control}</div>
    </div>
  );
}

export function Toggle({
  id,
  checked,
  onChange,
  disabled = false,
  label,
}: {
  id?: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={`settings-toggle ${checked ? "settings-toggle--on" : ""}`}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span className="settings-toggle__thumb" aria-hidden="true" />
    </button>
  );
}

export function NumberField({
  id,
  value,
  onChange,
  min,
  max,
  step = 1,
  unit,
  disabled = false,
  width = "md",
  "aria-label": ariaLabel,
}: {
  id?: string;
  value: number | "";
  onChange: (value: number | "") => void;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  disabled?: boolean;
  width?: "sm" | "md" | "lg";
  "aria-label"?: string;
}) {
  const invalid = typeof value === "number"
    && ((min !== undefined && value < min) || (max !== undefined && value > max));
  return (
    <span className={`settings-number settings-number--${width} ${invalid ? "settings-number--invalid" : ""}`}>
      <input
        id={id}
        type="number"
        inputMode="decimal"
        value={value}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-invalid={invalid || undefined}
        onChange={(event) => {
          const raw = event.target.value;
          if (raw === "") { onChange(""); return; }
          const next = Number(raw);
          if (Number.isFinite(next)) onChange(next);
        }}
      />
      {unit ? <span className="settings-number__unit">{unit}</span> : null}
    </span>
  );
}

export function TextField({
  id,
  value,
  onChange,
  placeholder,
  disabled = false,
  mono = false,
  "aria-label": ariaLabel,
}: {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  mono?: boolean;
  "aria-label"?: string;
}) {
  return (
    <input
      id={id}
      type="text"
      className={`settings-text ${mono ? "settings-text--mono" : ""}`}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      aria-label={ariaLabel}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function SegmentedControl<T extends string>({
  value,
  options,
  onChange,
  "aria-label": ariaLabel,
  disabled = false,
}: {
  value: T;
  options: readonly { value: T; label: string; description?: string }[];
  onChange: (value: T) => void;
  "aria-label"?: string;
  disabled?: boolean;
}) {
  const groupId = useId();
  return (
    <div className="settings-segmented" role="radiogroup" aria-label={ariaLabel}>
      {options.map((option) => (
        <button
          key={option.value}
          id={`${groupId}-${option.value}`}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          className={`settings-segmented__option ${value === option.value ? "settings-segmented__option--active" : ""}`}
          title={option.description}
          disabled={disabled}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function SettingsCard({
  title,
  description,
  children,
  actions,
  id,
}: {
  title?: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
  id?: string;
}) {
  return (
    <section className="settings-card" id={id} aria-labelledby={id ? `${id}-title` : undefined}>
      {title || actions ? (
        <header className="settings-card__header">
          <div>
            {title ? <h3 id={id ? `${id}-title` : undefined}>{title}</h3> : null}
            {description ? <p>{description}</p> : null}
          </div>
          {actions ? <div className="settings-card__actions">{actions}</div> : null}
        </header>
      ) : null}
      <div className="settings-card__body">{children}</div>
    </section>
  );
}

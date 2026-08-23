"use client";

/**
 * Settings controls — the form primitives the Settings page composes:
 * a row, a toggle, a number field, a text field, a segmented control,
 * and a card. Every control keeps a 36px hit target; the visible shape can
 * be smaller (the toggle track is 40×22 inside a 36px-tall button).
 */

import { useId, useState, type ReactNode } from "react";
import { RotateCcw } from "lucide-react";

export function SettingsRow({
  label,
  description,
  control,
  htmlFor,
  overridden = false,
  changed = false,
  error,
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
  /** Validation message for the current draft value. */
  error?: string;
  onReset?: () => void;
  resetLabel?: string;
  align?: "center" | "start";
}) {
  const Label = htmlFor ? "label" : "div";
  return (
    <div
      className={`settings-row ${changed ? "settings-row--changed" : ""} ${error ? "settings-row--invalid" : ""}`}
      data-align={align}
    >
      <div className="settings-row__text">
        <Label className="settings-row__label" {...(htmlFor ? { htmlFor } : {})}>
          {label}
          {overridden ? <span className="settings-pill settings-pill--session" title="This value is a session override. It resets when the daemon restarts.">Session</span> : null}
          {changed ? <span className="settings-pill settings-pill--changed">Unsaved</span> : null}
        </Label>
        {description ? <p className="settings-row__description">{description}</p> : null}
        {error ? <p className="settings-row__error" role="alert">{error}</p> : null}
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
      <span className="settings-toggle__track" aria-hidden="true">
        <span className="settings-toggle__thumb" />
      </span>
    </button>
  );
}

/**
 * Number input with its own text state. The parent receives a number only
 * when the text parses; clearing the field or typing a partial value does
 * not reset the field. Integer fields round on blur. The mouse wheel never
 * changes the value.
 */
export function NumberField({
  id,
  value,
  onChange,
  min,
  max,
  step = 1,
  integer = false,
  unit,
  disabled = false,
  invalid = false,
  width = "md",
  "aria-label": ariaLabel,
  "aria-describedby": ariaDescribedBy,
}: {
  id?: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  integer?: boolean;
  unit?: string;
  disabled?: boolean;
  invalid?: boolean;
  width?: "sm" | "md" | "lg";
  "aria-label"?: string;
  "aria-describedby"?: string;
}) {
  const [text, setText] = useState(String(value));
  const [focused, setFocused] = useState(false);
  const [syncedValue, setSyncedValue] = useState(value);
  // Adopt a new outer value (reset, refresh, discard) while the field is not being edited.
  if (!focused && syncedValue !== value) {
    setSyncedValue(value);
    setText(String(value));
  }

  const commit = (raw: string) => {
    setText(raw);
    if (raw.trim() === "" || raw === "-" || raw === ".") return;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return;
    onChange(integer ? Math.trunc(parsed) : parsed);
  };

  return (
    <span className={`settings-number settings-number--${width} ${invalid ? "settings-number--invalid" : ""}`}>
      <input
        id={id}
        type="number"
        inputMode={integer ? "numeric" : "decimal"}
        value={text}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-describedby={ariaDescribedBy}
        aria-invalid={invalid || undefined}
        onFocus={() => setFocused(true)}
        onBlur={() => {
          setFocused(false);
          setText(String(value));
        }}
        onWheel={(event) => event.currentTarget.blur()}
        onChange={(event) => commit(event.target.value)}
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
  invalid = false,
  mono = false,
  "aria-label": ariaLabel,
}: {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  invalid?: boolean;
  mono?: boolean;
  "aria-label"?: string;
}) {
  return (
    <input
      id={id}
      type="text"
      className={`settings-text ${mono ? "settings-text--mono" : ""} ${invalid ? "settings-text--invalid" : ""}`}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      aria-label={ariaLabel}
      aria-invalid={invalid || undefined}
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
  const move = (direction: 1 | -1) => {
    const index = options.findIndex((option) => option.value === value);
    const next = options[(index + direction + options.length) % options.length];
    if (next) onChange(next.value);
  };
  return (
    <div className="settings-segmented" role="radiogroup" aria-label={ariaLabel}>
      {options.map((option) => {
        const active = value === option.value;
        return (
          <button
            key={option.value}
            id={`${groupId}-${option.value}`}
            type="button"
            role="radio"
            aria-checked={active}
            tabIndex={active ? 0 : -1}
            className={`settings-segmented__option ${active ? "settings-segmented__option--active" : ""}`}
            title={option.description}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowRight" || event.key === "ArrowDown") { event.preventDefault(); move(1); }
              if (event.key === "ArrowLeft" || event.key === "ArrowUp") { event.preventDefault(); move(-1); }
            }}
          >
            {option.label}
          </button>
        );
      })}
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

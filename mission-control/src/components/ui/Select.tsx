"use client";

/**
 * Select — a drop-in replacement for the native <select>.
 *
 * It accepts the same `<option>` children and calls `onChange` with an
 * event-like object (`event.target.value`), so existing form code keeps
 * working. The menu itself is the styled SelectMenu listbox.
 */

import React, { type ReactNode } from "react";
import { SelectMenu, type SelectOption } from "./SelectMenu";

export interface SelectChangeEvent {
  target: { value: string; name?: string };
}

export interface SelectProps {
  value?: string | number;
  defaultValue?: string | number;
  onChange?: (event: SelectChangeEvent) => void;
  children?: ReactNode;
  id?: string;
  name?: string;
  required?: boolean;
  disabled?: boolean;
  className?: string;
  size?: "sm" | "md";
  "aria-label"?: string;
  "aria-describedby"?: string;
  title?: string;
}

function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (React.isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

export function optionsFromChildren(children: ReactNode): SelectOption[] {
  const options: SelectOption[] = [];
  React.Children.forEach(children, (child) => {
    if (!React.isValidElement(child)) return;
    const props = child.props as { value?: string | number; disabled?: boolean; children?: ReactNode; label?: string };
    if (child.type === React.Fragment) {
      options.push(...optionsFromChildren(props.children));
      return;
    }
    if (child.type === "optgroup") {
      options.push(...optionsFromChildren(props.children));
      return;
    }
    if (child.type !== "option") return;
    const label = textOf(props.children) || props.label || "";
    const value = props.value === undefined ? label : String(props.value);
    options.push({ value, label: label || value, disabled: props.disabled });
  });
  return options;
}

export function Select({
  value,
  defaultValue,
  onChange,
  children,
  id,
  name,
  required = false,
  disabled = false,
  className = "",
  size = "md",
  title,
  ...aria
}: SelectProps) {
  const options = React.useMemo(() => optionsFromChildren(children), [children]);
  const [internal, setInternal] = React.useState(
    defaultValue === undefined ? options[0]?.value ?? "" : String(defaultValue),
  );
  const controlled = value !== undefined;
  const current = controlled ? String(value) : internal;
  return (
    <span className={`select-field ${className}`} title={title}>
      <SelectMenu
        id={id}
        value={current}
        options={options}
        size={size}
        disabled={disabled}
        aria-label={aria["aria-label"]}
        aria-describedby={aria["aria-describedby"]}
        className="select-field__menu"
        onChange={(next) => {
          if (!controlled) setInternal(next);
          onChange?.({ target: { value: next, name } });
        }}
      />
      {required ? (
        <input
          className="select-field__required"
          tabIndex={-1}
          aria-hidden="true"
          required
          value={current}
          onChange={() => undefined}
          name={name}
        />
      ) : null}
    </span>
  );
}

export default Select;

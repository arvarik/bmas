"use client";

/**
 * SelectMenu — a styled, accessible replacement for the native <select>.
 *
 * The trigger is a button with `role="combobox"`. The popup is a listbox
 * rendered in a portal so that panels with `overflow: hidden` do not clip it.
 * Keyboard: ArrowUp/ArrowDown move, Home/End jump, Enter/Space select,
 * Escape closes, typing jumps to the first matching option.
 */

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown } from "lucide-react";

export interface SelectOption {
  value: string;
  label: string;
  description?: string;
  disabled?: boolean;
  icon?: ReactNode;
}

export interface SelectMenuProps {
  value: string;
  options: readonly SelectOption[];
  onChange: (value: string) => void;
  /** Accessible name when no visible <label> points at `id`. */
  "aria-label"?: string;
  "aria-describedby"?: string;
  id?: string;
  placeholder?: string;
  /** `field` looks like an input; `pill` is a compact rounded chip. */
  variant?: "field" | "pill";
  size?: "sm" | "md";
  align?: "start" | "end";
  disabled?: boolean;
  className?: string;
  /** Text shown before the selected label inside the trigger. */
  prefix?: ReactNode;
  /** Render a custom trigger label (defaults to the selected option label). */
  renderValue?: (option: SelectOption | undefined) => ReactNode;
}

const MENU_GAP = 6;
const VIEWPORT_MARGIN = 8;

interface MenuPosition {
  top: number;
  left: number;
  minWidth: number;
  maxHeight: number;
  placement: "below" | "above";
}

export function SelectMenu({
  value,
  options,
  onChange,
  id,
  placeholder = "Select…",
  variant = "field",
  size = "md",
  align = "start",
  disabled = false,
  className = "",
  prefix,
  renderValue,
  ...aria
}: SelectMenuProps) {
  const generatedId = useId();
  const listboxId = `${id ?? generatedId}-listbox`;
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const typeahead = useRef({ text: "", at: 0 });
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [position, setPosition] = useState<MenuPosition | null>(null);

  const selectedIndex = useMemo(() => options.findIndex((option) => option.value === value), [options, value]);
  const selected = selectedIndex >= 0 ? options[selectedIndex] : undefined;

  const close = useCallback((restoreFocus = true) => {
    setOpen(false);
    setPosition(null);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  const openMenu = useCallback(() => {
    if (disabled || options.length === 0) return;
    const firstEnabled = options.findIndex((option) => !option.disabled);
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : firstEnabled);
    setOpen(true);
  }, [disabled, options, selectedIndex]);

  // Position the popup from the trigger rectangle.
  useLayoutEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const viewportHeight = window.innerHeight;
    const viewportWidth = window.innerWidth;
    const spaceBelow = viewportHeight - rect.bottom - MENU_GAP - VIEWPORT_MARGIN;
    const spaceAbove = rect.top - MENU_GAP - VIEWPORT_MARGIN;
    const placement: MenuPosition["placement"] = spaceBelow < 180 && spaceAbove > spaceBelow ? "above" : "below";
    const maxHeight = Math.max(120, Math.min(320, placement === "below" ? spaceBelow : spaceAbove));
    const minWidth = Math.max(rect.width, 160);
    let left = align === "end" ? rect.right - minWidth : rect.left;
    left = Math.min(Math.max(VIEWPORT_MARGIN, left), viewportWidth - minWidth - VIEWPORT_MARGIN);
    setPosition({
      top: placement === "below" ? rect.bottom + MENU_GAP : rect.top - MENU_GAP,
      left,
      minWidth,
      maxHeight,
      placement,
    });
  }, [align, open]);

  // Close on outside pointer, scroll, or resize.
  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (triggerRef.current?.contains(target) || listRef.current?.contains(target)) return;
      close(false);
    }
    function handleScroll(event: Event) {
      if (listRef.current?.contains(event.target as Node)) return;
      close(false);
    }
    document.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("scroll", handleScroll, true);
    window.addEventListener("resize", handleScroll);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("scroll", handleScroll, true);
      window.removeEventListener("resize", handleScroll);
    };
  }, [close, open]);

  // Move focus into the list and keep the active option visible.
  useEffect(() => {
    if (!open) return;
    listRef.current?.focus({ preventScroll: true });
  }, [open]);

  useEffect(() => {
    if (!open || activeIndex < 0) return;
    const item = listRef.current?.children[activeIndex] as HTMLElement | undefined;
    item?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open]);

  const moveActive = useCallback((direction: 1 | -1) => {
    if (options.length === 0) return;
    let next = activeIndex;
    for (let step = 0; step < options.length; step += 1) {
      next = (next + direction + options.length) % options.length;
      if (!options[next].disabled) break;
    }
    setActiveIndex(next);
  }, [activeIndex, options]);

  const commit = useCallback((index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    if (option.value !== value) onChange(option.value);
    close();
  }, [close, onChange, options, value]);

  const jumpToText = useCallback((key: string) => {
    const now = Date.now();
    const text = now - typeahead.current.at < 600 ? typeahead.current.text + key : key;
    typeahead.current = { text: text.toLowerCase(), at: now };
    const start = activeIndex >= 0 ? activeIndex + 1 : 0;
    for (let offset = 0; offset < options.length; offset += 1) {
      const index = (start + offset) % options.length;
      const option = options[index];
      if (!option.disabled && option.label.toLowerCase().startsWith(typeahead.current.text)) {
        setActiveIndex(index);
        return;
      }
    }
  }, [activeIndex, options]);

  const handleTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return;
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      openMenu();
    }
  };

  const handleListKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActive(1);
        break;
      case "ArrowUp":
        event.preventDefault();
        moveActive(-1);
        break;
      case "Home":
        event.preventDefault();
        setActiveIndex(options.findIndex((option) => !option.disabled));
        break;
      case "End":
        event.preventDefault();
        for (let index = options.length - 1; index >= 0; index -= 1) {
          if (!options[index].disabled) { setActiveIndex(index); break; }
        }
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        commit(activeIndex);
        break;
      case "Escape":
        event.preventDefault();
        close();
        break;
      case "Tab":
        close(false);
        break;
      default:
        if (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
          event.preventDefault();
          jumpToText(event.key);
        }
    }
  };

  const triggerContent = renderValue
    ? renderValue(selected)
    : selected
      ? (
        <>
          {selected.icon ? <span className="select-menu__icon" aria-hidden="true">{selected.icon}</span> : null}
          <span className="select-menu__value">{selected.label}</span>
        </>
      )
      : <span className="select-menu__value select-menu__value--placeholder">{placeholder}</span>;

  return (
    <>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        className={`select-menu select-menu--${variant} select-menu--${size} ${open ? "select-menu--open" : ""} ${className}`}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-label={aria["aria-label"]}
        aria-describedby={aria["aria-describedby"]}
        disabled={disabled}
        onClick={() => (open ? close() : openMenu())}
        onKeyDown={handleTriggerKeyDown}
      >
        {prefix ? <span className="select-menu__prefix">{prefix}</span> : null}
        {triggerContent}
        <ChevronDown size={size === "sm" ? 13 : 15} className="select-menu__chevron" aria-hidden="true" />
      </button>
      {open && position && typeof document !== "undefined"
        ? createPortal(
          <ul
            ref={listRef}
            id={listboxId}
            className={`select-menu__list select-menu__list--${position.placement}`}
            role="listbox"
            tabIndex={-1}
            aria-activedescendant={activeIndex >= 0 ? `${listboxId}-${activeIndex}` : undefined}
            aria-label={aria["aria-label"]}
            style={{
              top: position.placement === "below" ? position.top : undefined,
              bottom: position.placement === "above" ? window.innerHeight - position.top : undefined,
              left: position.left,
              minWidth: position.minWidth,
              maxHeight: position.maxHeight,
            }}
            onKeyDown={handleListKeyDown}
          >
            {options.map((option, index) => (
              <li
                key={option.value}
                id={`${listboxId}-${index}`}
                role="option"
                aria-selected={option.value === value}
                aria-disabled={option.disabled || undefined}
                className={`select-menu__option ${index === activeIndex ? "select-menu__option--active" : ""} ${option.disabled ? "select-menu__option--disabled" : ""}`}
                onMouseEnter={() => { if (!option.disabled) setActiveIndex(index); }}
                onClick={() => commit(index)}
              >
                {option.icon ? <span className="select-menu__icon" aria-hidden="true">{option.icon}</span> : null}
                <span className="select-menu__option-text">
                  <span>{option.label}</span>
                  {option.description ? <small>{option.description}</small> : null}
                </span>
                {option.value === value ? <Check size={14} className="select-menu__check" aria-hidden="true" /> : null}
              </li>
            ))}
          </ul>,
          document.body,
        )
        : null}
    </>
  );
}

export default SelectMenu;

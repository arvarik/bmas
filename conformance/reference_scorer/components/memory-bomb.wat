;; An attack fixture: the scorer grows memory until the store limiter
;; refuses, then touches the page it never received.
(component
  (core module $m
    (memory (export "memory") 1)
    (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) (i32.const 8192))
    (func (export "score") (param i32 i32 i32 i32) (result i32)
      (local $pages i32)
      (block $stop
        (loop $grow
          (local.set $pages (memory.grow (i32.const 16)))
          (br_if $stop (i32.eq (local.get $pages) (i32.const -1)))
          (br $grow)))
      ;; Touch one byte past the current memory size: the trap proves
      ;; the guest never received the refused page.
      (i32.store8 (i32.mul (memory.size) (i32.const 65536)) (i32.const 1))
      (i32.const 1024))
  )
  (core instance $i (instantiate $m))
  (func (export "score") (param "expected" string) (param "actual" string) (result (tuple f64 bool))
    (canon lift (core func $i "score") (memory $i "memory") (realloc (func $i "cabi_realloc")) string-encoding=utf8))
)

;; An attack fixture: the scorer imports the WASI wall clock. The
;; boundary rejects the component before instantiation.
(component
  (import "wasi:clocks/wall-clock@0.2.0" (instance (export "now" (func (result u64)))))
  (core module $m
    (memory (export "memory") 1)
    (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) (i32.const 8192))
    (func (export "score") (param i32 i32 i32 i32) (result i32) (i32.const 1024))
  )
  (core instance $i (instantiate $m))
  (func (export "score") (param "expected" string) (param "actual" string) (result (tuple f64 bool))
    (canon lift (core func $i "score") (memory $i "memory") (realloc (func $i "cabi_realloc")) string-encoding=utf8))
)

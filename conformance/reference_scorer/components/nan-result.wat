;; A fixture that returns a NaN score: the host canonicalizes the NaN
;; payload so equal executions render equal bytes.
(component
  (core module $m
    (memory (export "memory") 1)
    (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) (i32.const 8192))
    (func (export "score") (param i32 i32 i32 i32) (result i32)
      (f64.store (i32.const 1024) (f64.div (f64.const 0) (f64.const 0)))
      (i32.store8 (i32.const 1032) (i32.const 0))
      (i32.const 1024))
  )
  (core instance $i (instantiate $m))
  (func (export "score") (param "expected" string) (param "actual" string) (result (tuple f64 bool))
    (canon lift (core func $i "score") (memory $i "memory") (realloc (func $i "cabi_realloc")) string-encoding=utf8))
)

;; An attack fixture: the scorer loops forever and exhausts its fuel.
(component
  (core module $m
    (memory (export "memory") 1)
    (func (export "cabi_realloc") (param i32 i32 i32 i32) (result i32) (i32.const 8192))
    (func (export "score") (param i32 i32 i32 i32) (result i32)
      (loop $forever (br $forever))
      (i32.const 1024))
  )
  (core instance $i (instantiate $m))
  (func (export "score") (param "expected" string) (param "actual" string) (result (tuple f64 bool))
    (canon lift (core func $i "score") (memory $i "memory") (realloc (func $i "cabi_realloc")) string-encoding=utf8))
)

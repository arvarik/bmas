;; The reference scorer component: exact byte equality of the expected
;; and actual strings. The component targets the bmas:scorer world and
;; imports only the granted logical-time and deterministic-random
;; interfaces, which it calls once each so the host links are exercised.
(component
  (import "bmas:scorer/logical-time@0.1.0" (instance $clock
    (export "now" (func (result u64)))))
  (import "bmas:scorer/deterministic-random@0.1.0" (instance $random
    (export "next-word" (func (result u64)))))
  (core func $now (canon lower (func $clock "now")))
  (core func $next_word (canon lower (func $random "next-word")))
  (core module $m
    (import "bmas:scorer/logical-time@0.1.0" "now" (func $now (result i64)))
    (import "bmas:scorer/deterministic-random@0.1.0" "next-word" (func $next_word (result i64)))
    (memory (export "memory") 1)
    (global $heap (mut i32) (i32.const 4096))
    (func (export "cabi_realloc") (param $old i32) (param $old_size i32) (param $align i32) (param $new_size i32) (result i32)
      (local $ptr i32)
      (local.set $ptr (global.get $heap))
      (global.set $heap (i32.add (local.get $ptr) (local.get $new_size)))
      (local.get $ptr))
    (func (export "score") (param $ep i32) (param $el i32) (param $ap i32) (param $al i32) (result i32)
      (local $i i32) (local $eq i32)
      ;; Read the logical clock and one random word; both land in memory
      ;; so the calls stay observable and the host links are exercised.
      (i64.store (i32.const 2048) (call $now))
      (i64.store (i32.const 2056) (call $next_word))
      (local.set $eq (i32.const 1))
      (if (i32.ne (local.get $el) (local.get $al)) (then (local.set $eq (i32.const 0))))
      (block $done
        (loop $again
          (br_if $done (i32.eqz (local.get $eq)))
          (br_if $done (i32.ge_u (local.get $i) (local.get $el)))
          (if (i32.ne (i32.load8_u (i32.add (local.get $ep) (local.get $i)))
                      (i32.load8_u (i32.add (local.get $ap) (local.get $i))))
            (then (local.set $eq (i32.const 0))))
          (local.set $i (i32.add (local.get $i) (i32.const 1)))
          (br $again)))
      (f64.store (i32.const 1024) (f64.convert_i32_u (local.get $eq)))
      (i32.store8 (i32.const 1032) (local.get $eq))
      (i32.const 1024))
  )
  (core instance $i (instantiate $m
    (with "bmas:scorer/logical-time@0.1.0" (instance (export "now" (func $now))))
    (with "bmas:scorer/deterministic-random@0.1.0" (instance (export "next-word" (func $next_word))))))
  (func (export "score") (param "expected" string) (param "actual" string) (result (tuple f64 bool))
    (canon lift (core func $i "score") (memory $i "memory") (realloc (func $i "cabi_realloc")) string-encoding=utf8))
)

# LLM Prompt Template: check_side_effect Verification

> **Purpose**: Stage 3 (LLM) verification for check() macro side effects
> **Input**: Code snippet with check() usage flagged by Stage 1 regex filter
> **Output**: Verification result (safe/unsafe) + suggestion

---

## 🎯 Prompt Template

```markdown
# Task: Verify Side Effects in check() Macro

## Context
In Unreal Engine 5, the `check()` macro is **completely removed** in Shipping builds:
```cpp
#if DO_CHECK  // Development, Debug
  #define check(expr) { if (!(expr)) { FDebug::AssertFailed(...); } }
#else         // Shipping, Test
  #define check(expr) {}  // ← Expression is NOT evaluated!
#endif

#define verify(expr) { if (!(expr)) { FDebug::AssertFailed(...); } }  // Always evaluated
```

**Critical Issue**: If `check()` contains side effects, the code will behave differently in Shipping builds.

---

## Side Effect Definition

### ✅ Side Effect Present (UNSAFE for check)
Code that modifies program state or has observable effects:
- Variable modification: `++i`, `x = 5`, `x += 10`
- Function calls that change state: `ProcessItem()`, `AddToQueue()`
- I/O operations: File writes, network calls (rare in check)
- Memory allocation: `new`, `NewObject` (rare in check)

### ❌ No Side Effect (SAFE for check)
Pure read-only operations:
- Variable reads: `x == 5`, `Index < MaxCount`
- Comparison operators: `!=`, `>`, `<=`
- Pure getter functions: `IsValid()`, `GetNum()`, `GetClass()`
- Type checks: `IsA()`, `ImplementsInterface()`

---

## Known Safe Functions (Whitelist)

The following UE5 functions are **guaranteed side-effect free**:

{{SAFE_FUNCTIONS_LIST}}

**Note**: Functions NOT in this list require analysis of their implementation.

---

## Analysis Instructions

1. **Identify the check() expression** in the code snippet
2. **Analyze each component** for side effects:
   - Increment/decrement operators (`++`, `--`)
   - Assignment operators (`=`, `+=`, `-=`, etc.)
   - Function calls (check against whitelist)
3. **Consider Shipping build impact**: Will removing this expression change behavior?
4. **Provide verdict**: SAFE or UNSAFE
5. **Generate suggestion** if UNSAFE

---

## Code to Analyze

```cpp
{{CODE_SNIPPET}}
```

**File**: {{FILE_PATH}}
**Line**: {{LINE_NUMBER}}

---

## Output Format

Please respond in the following JSON format:

```json
{
  "verdict": "SAFE" | "UNSAFE",
  "confidence": 0.0-1.0,
  "reasoning": "Brief explanation of the analysis",
  "side_effects_found": [
    {
      "type": "increment" | "assignment" | "function_call" | "other",
      "location": "++Index",
      "description": "Index variable is incremented"
    }
  ],
  "shipping_impact": "Description of what breaks in Shipping build",
  "suggestion": {
    "action": "use_verify" | "split_logic" | "none",
    "code": "Corrected code snippet",
    "explanation": "Why this fix works"
  }
}
```

---

## Example 1: Increment Operator (UNSAFE)

### Input
```cpp
int Index = 0;
check(++Index < MaxCount);
```

### Output
```json
{
  "verdict": "UNSAFE",
  "confidence": 1.0,
  "reasoning": "The ++Index operator modifies the Index variable, which is a side effect.",
  "side_effects_found": [
    {
      "type": "increment",
      "location": "++Index",
      "description": "Index is incremented before the comparison"
    }
  ],
  "shipping_impact": "In Shipping builds, Index will NOT be incremented, causing the loop or logic to malfunction.",
  "suggestion": {
    "action": "use_verify",
    "code": "verify(++Index < MaxCount);",
    "explanation": "verify() executes in all build configurations, ensuring Index is always incremented."
  }
}
```

---

## Example 2: IsValid() (SAFE)

### Input
```cpp
AActor* Actor = GetOwner();
check(IsValid(Actor));
```

### Output
```json
{
  "verdict": "SAFE",
  "confidence": 1.0,
  "reasoning": "IsValid() is a pure read-only function that checks pointer validity without modifying state.",
  "side_effects_found": [],
  "shipping_impact": "None. IsValid() does not change program state.",
  "suggestion": {
    "action": "none",
    "code": null,
    "explanation": "The current usage is correct. check() is appropriate for pure assertions."
  }
}
```

---

## Example 3: Unknown Function (Requires Analysis)

### Input
```cpp
bool ProcessNextItem() {
    CurrentIndex++;  // ← Side effect!
    return CurrentIndex < Items.Num();
}

check(ProcessNextItem());
```

### Output
```json
{
  "verdict": "UNSAFE",
  "confidence": 0.95,
  "reasoning": "ProcessNextItem() modifies CurrentIndex, which is a side effect. This function is not in the safe functions whitelist.",
  "side_effects_found": [
    {
      "type": "function_call",
      "location": "ProcessNextItem()",
      "description": "Function modifies CurrentIndex member variable"
    }
  ],
  "shipping_impact": "In Shipping builds, ProcessNextItem() will NOT be called, so CurrentIndex will not advance, breaking iteration logic.",
  "suggestion": {
    "action": "split_logic",
    "code": "const bool bProcessed = ProcessNextItem();\ncheck(bProcessed);",
    "explanation": "Separate the side-effect (ProcessNextItem call) from the assertion. This ensures the function is always called, even in Shipping. Alternatively, use verify(ProcessNextItem()) if the assertion is critical."
  }
}
```

---

## Example 4: Getter Chain (SAFE)

### Input
```cpp
ACharacter* Character = Cast<ACharacter>(Actor);
check(Character && Character->GetMovementComponent() && Character->GetMovementComponent()->IsActive());
```

### Output
```json
{
  "verdict": "SAFE",
  "confidence": 0.98,
  "reasoning": "All functions in the chain are read-only getters: GetMovementComponent() returns a pointer, IsActive() checks a boolean flag. No state is modified.",
  "side_effects_found": [],
  "shipping_impact": "None. All operations are read-only.",
  "suggestion": {
    "action": "none",
    "code": null,
    "explanation": "The usage is correct. However, consider simplifying for readability: verify(Character) and then check the movement component separately."
  }
}
```

---

## Example 5: Assignment (UNSAFE)

### Input
```cpp
int Result;
check((Result = ComputeValue()) > 0);
```

### Output
```json
{
  "verdict": "UNSAFE",
  "confidence": 1.0,
  "reasoning": "The assignment operator (Result = ComputeValue()) modifies the Result variable, which is a side effect.",
  "side_effects_found": [
    {
      "type": "assignment",
      "location": "Result = ComputeValue()",
      "description": "Result variable is assigned within the check expression"
    }
  ],
  "shipping_impact": "In Shipping builds, ComputeValue() will NOT be called, and Result will remain uninitialized or have a stale value.",
  "suggestion": {
    "action": "split_logic",
    "code": "Result = ComputeValue();\ncheck(Result > 0);",
    "explanation": "Separate the assignment from the assertion. This ensures ComputeValue() is always called and Result is always assigned."
  }
}
```

---

## Special Cases

### Case 1: Complex Boolean Expressions
```cpp
check(A && B || C && D);
```
**Analysis**: If A, B, C, D are all variable reads or safe function calls, this is SAFE.

### Case 2: Ternary Operator
```cpp
check((Count > 0) ? IsValid(Items[0]) : true);
```
**Analysis**: Safe if both branches are side-effect free.

### Case 3: Lambda Functions
```cpp
check([&]() { return Index < MaxCount; }());
```
**Analysis**: Check lambda body for side effects (captures, modifications).

---

## Implementation Notes

### For Stage 3 LLM Integration
1. Load `configs/safe_functions.yml` and inject into `{{SAFE_FUNCTIONS_LIST}}`
2. Extract code snippet and metadata (file, line number)
3. Send prompt to LLM (Claude, GPT-4, etc.)
4. Parse JSON response
5. Report result to user (GitHub PR comment, CI log, etc.)

### Confidence Threshold
- **>= 0.9**: Auto-apply verdict
- **0.7-0.9**: Flag for human review
- **< 0.7**: Request additional context or manual inspection

### Handling Uncertainty
If the LLM cannot determine side effects (e.g., function definition not found):
```json
{
  "verdict": "UNCERTAIN",
  "confidence": 0.5,
  "reasoning": "Cannot analyze ProcessItem() - definition not found in context",
  "suggestion": {
    "action": "manual_review",
    "explanation": "Please verify that ProcessItem() is side-effect free, or use verify() to be safe."
  }
}
```

---

## Testing the Prompt

### Test Cases
Use these examples to validate prompt effectiveness:

| Test Case | Expected Verdict | Key Challenge |
|-----------|------------------|---------------|
| `check(++i < N)` | UNSAFE | Increment operator |
| `check(IsValid(X))` | SAFE | Whitelist function |
| `check(X->Num() > 0)` | SAFE | Container getter |
| `check(ProcessItem())` | UNSAFE | Unknown function (not whitelist) |
| `check((x = 5) > 0)` | UNSAFE | Assignment in expression |
| `check(A && B)` | SAFE | Boolean reads |

### Metrics
- **Precision**: % of UNSAFE verdicts that are actually unsafe
- **Recall**: % of actual unsafe cases correctly identified
- **F1 Score**: Harmonic mean of precision and recall

Target: **Precision >= 0.95, Recall >= 0.90**

---

## Change Log

| Date | Author | Changes |
|------|--------|---------|
| 2026-02-13 | Claude | Initial prompt template with examples and JSON schema |

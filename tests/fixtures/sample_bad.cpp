// sample_bad.cpp — 의도적 규칙 위반 샘플 (Stage 1 + Stage 3 검증용)
// 이 파일은 테스트 용도로만 사용됩니다. 실제 프로젝트 코드가 아닙니다.

#include "MyActor.h"
#include "Engine/Engine.h"

#define LOCTEXT_NAMESPACE "MyModule"

// ============================================================================
// Stage 1 (regex로 검출) — Tier 1 패턴
// ============================================================================

// [logtemp] LogTemp 사용
void AMyActor::BadLogging()
{
	UE_LOG(LogTemp, Warning, TEXT("This should use a custom log category"));
}

// [pragma_optimize_off] #pragma optimize off
#pragma optimize("", off)
void AMyActor::SlowFunction()
{
	// 디버깅용 최적화 비활성화 — 절대 커밋 금지
}
#pragma optimize("", on)

// [hard_asset_path] 하드코딩된 에셋 경로
void AMyActor::LoadAssetBad()
{
	FString Path = TEXT("/Game/Path/To/MyBlueprint.MyBlueprint_C");
	UClass* LoadedClass = StaticLoadClass(UObject::StaticClass(), nullptr, *Path);
}

// [macro_no_semicolon] 매크로 뒤 세미콜론 누락
void AMyActor::MissingSemicolon()
{
	UE_LOG(LogTemp, Log, TEXT("Missing semicolon"))
	check(IsValid(this))
}

// [declaration_macro_semicolon] 선언 매크로 뒤 불필요한 세미콜론
UCLASS()
class MYGAME_API AMyBadActor : public AActor
{
	GENERATED_BODY();  // 불필요한 세미콜론

	UPROPERTY(EditAnywhere, meta=(AllowPrivateAccess="true"));  // 불필요한 세미콜론
	int32 BadProperty;
};

// [check_side_effect_suspicious] check() 내 부작용
void AMyActor::CheckSideEffects()
{
	int32 Index = 0;
	check(++Index < 10)
	check(ProcessItem(SomeItem))
}

// [sync_load_runtime] 런타임 동기 로딩
void AMyActor::SyncLoadBad()
{
	UObject* Obj = LoadObject<UStaticMesh>(nullptr, TEXT("/Game/Meshes/MyMesh"));
	UObject* Obj2 = StaticLoadObject(UStaticMesh::StaticClass(), nullptr, TEXT("/Game/Meshes/MyMesh2"));
}

// ============================================================================
// Stage 3 (LLM으로 검출) — 이관 항목
// ============================================================================

// [auto_non_lambda] auto 사용 (람다 아닌 곳)
void AMyActor::AutoUsage()
{
	auto Value = GetSomeValue();      // auto 금지 (람다가 아님)
	auto* Ptr = GetSomePointer();     // auto 금지

	auto Lambda = [this]() { return 42; };  // 이건 OK (람다)
}

// [yoda_condition] 요다 컨디션
void AMyActor::YodaStyle()
{
	bool bFlag = true;
	if (false == bFlag)
	{
		// 요다 스타일 금지
	}

	if (5 == GetCount())
	{
		// 상수가 왼쪽에 있으면 안 됨
	}
}

// [not_operator_in_if] ! 연산자 사용
void AMyActor::NotOperator()
{
	bool bFlag = true;
	if (!bFlag)
	{
		// bFlag == false 로 작성해야 함
	}
}

// [fsimpledelegate] FSimpleDelegateGraphTask 사용
void AMyActor::SimpleDelegateBad()
{
	FSimpleDelegateGraphTask::CreateAndDispatchWhenReady(
		FSimpleDelegateGraphTask::FDelegate::CreateLambda([]()
		{
			// 명시적 시그니처를 사용해야 함
		}),
		GET_STATID(STAT_MyTask)
	);
}

// [uobject_uproperty] UPROPERTY 없는 UObject* 멤버
class FMyBadClass
{
	UObject* DanglingPtr;  // UPROPERTY 없이 UObject* 보관 → GC 문제
};

// [tick_rpc] 매 Tick RPC 호출
void AMyActor::Tick(float DeltaTime)
{
	Super::Tick(DeltaTime);
	ServerDoSomething();  // 매 Tick마다 RPC 호출 — 네트워크 대역폭 폭발
}

// [transient_missing] Transient 없는 런타임 UPROPERTY
UPROPERTY(VisibleAnywhere)
float CachedDistance;  // 런타임 전용이면 Transient 필요

// [getworld_null_check] GetWorld() null 체크 없이 사용
void AMyActor::NoWorldCheck()
{
	GetWorld()->SpawnActor<AActor>(ActorClass);  // null 체크 없음!
}

// [loctext_no_undef] #undef LOCTEXT_NAMESPACE 누락
// 파일 끝에 #undef LOCTEXT_NAMESPACE 가 없음!

// [constructorhelpers_outside_ctor] ConstructorHelpers 생성자 외부 사용
void AMyActor::BeginPlay()
{
	Super::BeginPlay();
	static ConstructorHelpers::FObjectFinder<UStaticMesh> MeshFinder(TEXT("/Game/Meshes/SM_Default"));
	// ConstructorHelpers는 생성자 내에서만 사용해야 함
}

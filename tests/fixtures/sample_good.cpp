// sample_good.cpp — 규칙 준수 샘플 (false positive 0 확인용)
// 이 파일은 테스트 용도로만 사용됩니다. 실제 프로젝트 코드가 아닙니다.

#include "MyActor.h"
#include "Engine/Engine.h"

#define LOCTEXT_NAMESPACE "MyModule"

DEFINE_LOG_CATEGORY_STATIC(LogMyActor, Log, All);

// ============================================================================
// Stage 1 규칙을 올바르게 준수한 코드
// ============================================================================

// [logtemp OK] 적절한 로그 카테고리 사용
void AMyActor::GoodLogging()
{
	UE_LOG(LogMyActor, Warning, TEXT("Using proper log category"));
}

// [pragma_optimize_off OK] #pragma optimize 사용 안 함
void AMyActor::OptimizedFunction()
{
	// 최적화 비활성화 없이 정상 구현
	int32 Result = ComputeExpensiveValue();
}

// [hard_asset_path OK] SoftObjectPtr / DataTable 사용
void AMyActor::LoadAssetGood()
{
	TSoftObjectPtr<UStaticMesh> MeshRef = AssetReference;
	MeshRef.Get();  // SoftObjectPtr로 관리 (비동기 로딩은 StreamableManager 사용)
}

// [macro_no_semicolon OK] 매크로 뒤 세미콜론 있음
void AMyActor::CorrectSemicolons()
{
	UE_LOG(LogMyActor, Log, TEXT("Proper semicolon"));
	check(this != nullptr);
	ensure(SomeCondition);
}

// [declaration_macro_semicolon OK] 선언 매크로 뒤 세미콜론 없음
UCLASS()
class MYGAME_API AMyGoodActor : public AActor
{
	GENERATED_BODY()

	UPROPERTY(EditAnywhere, meta=(AllowPrivateAccess="true"))
	int32 GoodProperty;
};

// [check_side_effect OK] check()에 부작용 없는 순수 조건만
void AMyActor::CheckCorrect()
{
	int32 Index = ComputeIndex();
	check(Index >= 0);
	check(Index < MaxIndex);
	verify(ProcessItem(SomeItem));  // verify는 부작용 OK
}

// [sync_load_runtime OK] 비동기 로딩 사용
void AMyActor::AsyncLoadGood()
{
	FStreamableManager& Manager = UAssetManager::GetStreamableManager();
	Manager.RequestAsyncLoad(SoftObjectPath, FStreamableDelegate::CreateUObject(this, &AMyActor::OnAssetLoaded));
}

// ============================================================================
// Stage 3 규칙을 올바르게 준수한 코드
// ============================================================================

// [auto OK] 람다에만 auto 사용
void AMyActor::AutoLambdaOnly()
{
	int32 Value = GetSomeValue();           // 명시적 타입
	UStaticMesh* Ptr = GetSomePointer();    // 명시적 포인터 타입

	auto Lambda = [this]() { return 42; };  // 람다 OK
	auto Callback = [](int32 X) -> bool { return X > 0; };  // 람다 OK
}

// [yoda_condition OK] 자연스러운 조건식
void AMyActor::NaturalCondition()
{
	bool bFlag = true;
	if (bFlag == false)
	{
		// 자연스러운 순서
	}

	if (GetCount() == 5)
	{
		// 변수가 왼쪽
	}
}

// [not_operator OK] == false 사용
void AMyActor::ExplicitFalseCheck()
{
	bool bFlag = true;
	if (bFlag == false)
	{
		// ! 대신 == false 사용
	}

	if (IsValid(SomePtr))  // IsValid는 예외 허용
	{
	}
}

// [fsimpledelegate OK] DECLARE_DELEGATE 사용
DECLARE_DELEGATE_RetVal(bool, FMyCustomDelegate);

void AMyActor::ExplicitDelegate()
{
	FMyCustomDelegate Delegate;
	Delegate.BindLambda([]() -> bool { return true; });
}

// [uobject_uproperty OK] UPROPERTY로 UObject* 관리
UPROPERTY()
UObject* ManagedPtr;

// [tick OK] Tick에서 RPC 호출 안 함
void AMyActor::Tick(float DeltaTime)
{
	Super::Tick(DeltaTime);
	UpdateLocalState(DeltaTime);  // 로컬 업데이트만
}

// [transient OK] 런타임 전용 UPROPERTY에 Transient
UPROPERTY(Transient, VisibleAnywhere)
float CachedDistance;

// [getworld OK] GetWorld() null 체크
void AMyActor::SafeWorldAccess()
{
	UWorld* World = GetWorld();
	if (World != nullptr)
	{
		World->SpawnActor<AActor>(ActorClass);
	}
}

// [loctext OK] #undef 있음
#undef LOCTEXT_NAMESPACE

// [constructorhelpers OK] 생성자 내에서 사용
AMyActor::AMyActor()
{
	static ConstructorHelpers::FObjectFinder<UStaticMesh> MeshFinder(DefaultMeshPath);
	if (MeshFinder.Succeeded())
	{
		MeshComponent->SetStaticMesh(MeshFinder.Object);
	}

	PrimaryActorTick.bCanEverTick = false;  // Tick 비활성화
}

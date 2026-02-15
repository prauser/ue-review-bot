// sample_network.cpp — 네트워크 위반 샘플
// 이 파일은 테스트 용도로만 사용됩니다. 실제 프로젝트 코드가 아닙니다.

#include "MyNetActor.h"
#include "Net/UnrealNetwork.h"

// ============================================================================
// 네트워크 특화 위반
// ============================================================================

// [replicated_property_condition] DOREPLIFETIME 조건 미설정
void AMyNetActor::GetLifetimeReplicatedProps(TArray<FLifetimeProperty>& OutLifetimeProps) const
{
	Super::GetLifetimeReplicatedProps(OutLifetimeProps);

	// BAD: 조건 없이 모든 프로퍼티 복제
	DOREPLIFETIME(AMyNetActor, Health);
	DOREPLIFETIME(AMyNetActor, Ammo);
	DOREPLIFETIME(AMyNetActor, bIsAlive);

	// GOOD: 조건부 복제 (아래는 올바른 예시)
	// DOREPLIFETIME_CONDITION(AMyNetActor, Health, COND_OwnerOnly);
}

// [reliable_abuse] Reliable 남용
UFUNCTION(Server, Reliable)
void AMyNetActor::ServerUpdatePosition(FVector NewPosition);
// 위치 업데이트처럼 빈번한 호출에 Reliable을 사용하면 안 됨
// Unreliable을 사용해야 함

UFUNCTION(Server, Reliable)
void AMyNetActor::ServerFireWeapon();
// 무기 발사 같은 빈번한 이벤트에도 Reliable 남용

UFUNCTION(Client, Reliable)
void AMyNetActor::ClientReceiveDamage(float DamageAmount);
// 데미지 표시에 Reliable 불필요

// [tick_replication] 매 Tick Replication 변수
UPROPERTY(Replicated)
FVector CurrentVelocity;  // 매 프레임 변경되는 값을 Replicate

UPROPERTY(Replicated)
FRotator CurrentRotation;  // 매 프레임 변경되는 값을 Replicate

void AMyNetActor::Tick(float DeltaTime)
{
	Super::Tick(DeltaTime);

	// BAD: 매 Tick마다 Replicated 변수 업데이트
	CurrentVelocity = GetVelocity();
	CurrentRotation = GetActorRotation();

	// BAD: 매 Tick마다 RPC 호출
	if (HasAuthority())
	{
		ServerUpdatePosition(GetActorLocation());
	}
}

// ============================================================================
// 올바른 네트워크 코드 예시 (비교용)
// ============================================================================

// GOOD: 조건부 복제
void AMyGoodNetActor::GetLifetimeReplicatedProps(TArray<FLifetimeProperty>& OutLifetimeProps) const
{
	Super::GetLifetimeReplicatedProps(OutLifetimeProps);

	DOREPLIFETIME_CONDITION(AMyGoodNetActor, Health, COND_OwnerOnly);
	DOREPLIFETIME_CONDITION(AMyGoodNetActor, Ammo, COND_OwnerOnly);
	DOREPLIFETIME_CONDITION(AMyGoodNetActor, bIsAlive, COND_None);
}

// GOOD: Unreliable 사용
UFUNCTION(Server, Unreliable)
void AMyGoodNetActor::ServerUpdatePosition(FVector NewPosition);

// GOOD: 타이머 기반 복제
void AMyGoodNetActor::BeginPlay()
{
	Super::BeginPlay();

	if (HasAuthority())
	{
		GetWorldTimerManager().SetTimer(
			ReplicationTimerHandle,
			this,
			&AMyGoodNetActor::ReplicateState,
			0.1f,  // 100ms 간격
			true
		);
	}
}

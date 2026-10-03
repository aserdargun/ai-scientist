# Broker yetkisiyle bağımsız özgün bütçe okuma

2026-10-01. Scientist tabanı `ea62c7eace862e998957e4bbed26876a73d6d4b3`.

`LabAOSControl.read_original_budget()` mevcut ControlStore özgün
intent/allocation/readiness okumasını gerçek broker'ın retained-target
yetkilendirmesine bağlar. Bu trusted in-process API'dir; yeni socket operasyonu,
capability, control ID, inference veya GPU tahsis otoritesi değildir.

```python
witness = control.read_original_budget(
    authenticated_peer,
    exact_original_target,
    original_profile_id,
    original_deployment_digest,
    expected_capability_sha256=retained_target_capability_sha256,
)
```

Hash, başarılı target discovery yanıtındaki bütün `data.capability` nesnesinin
hash'idir. Bootstrap infer capability veya admission-binding hash'i kaynak okuma
hakkı vermez. Caller peer hash, target, profile ve deployment özgün kayda bağlıdır.
Retained target normal binding veya explicit cleanup ACL yolu kullanılır;
reconcile hakkı, policy/source/profile pinleri, peer/server generation ve expiry
yeniden doğrulanır. Özgün budget kolonları transaction içinde okunur ve dönüşten
önce current authority/peer tekrar kontrol edilir. Kontrol kotası/ID veya özgün
kayıt/süreler değişmez. Socket reconcile de aynı authority helper'ını kullanır.

## Kanıt ve kullanım sınırı

API yokken üç yeni kontrol exit1 verdi. Uygulamadan sonra mevcut socket,
retained-evidence ve özgün Store bütçe kontrolleriyle **24 passed / exit0**.
API özgün kolonlarla aynı witness üretir ve tekrar okumada control ID tüketmez;
bootstrap capability ve okuma sonrası peer revocation reddedilir. Önceki Store
kontrolleri terminal JSON içindeki budget değişikliğinin özgün kolonlardan
okumayı değiştirmediğini ayrıca doğrular. Yetki fixture'ları sentetiktir;
bu sonuç canlı systemd principal veya gerçek GPU kanıtı değildir.

Bu API, trusted parent'ın bağımsız kaynak doğrulamasını sağlar. Ayrı Python
ortamındaki AOS `verify_source(request, original, witness)` callback'ine aynı
target/current peer altında authenticated provider bağlantısı hâlâ gerekir.
Socket witness'ını aynı yanıtın terminaliyle karşılaştırmak bu bağlantının
yerine geçmez. Physical cleanup ve current resolution authority ayrıca zorunlu.
`SystemdAOSProfileRuntime.verify_drained()` salt okunur physical provider yerine
kullanılamaz: süreç durdurabilir ve drain kaydı yazabilir.

Ölçülmüş model/quantization/context, VRAM peak veya GPU devir gecikmesi yok.
Canonical broker/scheduler mevcut olmadığından gerçek GPU koşusu başlatılmadı.
AOS dosyaları/süreçleri değişmedi; deploy/push/merge yapılmadı. Kabul toplamları
11 geçti/7 kısmi/4 açık olarak korunur. Lisans/genel CI işi ayrı kalır.

## AOS oturumuna aktarım

Scientist trusted source read API'si hazır: retained target capability hash'i
ve authenticated peer ile `LabAOSControl.read_original_budget(...)`. Bütçe
terminalden değil özgün Store kolonlarından okunur. Cross-process `verify_source`
provider'ı bu API'nin independently authenticated current okumasına bağlanmalı;
physical/resolver/resolution callback'leri ayrı ve default deny kalmalı.

[Makine kaydı](review-evidence/trusted-retained-budget-reader.json)

## Kalite kapısı

Son zorunlu kapı yedi exit0; **1805 passed / 7 opt-in skipped /
120 GPU-live deselected**, parent139,764783803 saniye, kaynaklar sabit.
İlk kapıda yalnız mypy dönüş tipi bildirimi reddedildi; explicit Store
return annotation düzeltildi. Başarısız kapı kanıtı korundu.

# AOS resolution transaction'ına bağımsız broker readback bağlantısı

2026-10-01; Scientist tabanı `737b80b67167287f84476f6999fbf1e854a160ca`,
actual AOS HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.

## Giderilen gerçek bağlantı engeli

AOS `ScientistResolutionJournal.resolve()` doğrulayıcı çalışırken kendi SQLite
`BEGIN IMMEDIATE` transaction'ını tutar. İçeriden yeni journal'lı reconcile
çağırmak mevcut transaction ve host operation lock'ıyla çakışır. Transaction'ı
callback içinde kapatmak veya başarılı eski ACK'yi bağımsız kaynak saymak çözüm
değildir.

Yeni provider aynı canonical broker sürecindeki mevcut `LabAOSControl` nesnesini
kullanır. Bir inherited Linux Unix `SOCK_SEQPACKET` socketpair üzerinden yalnız
`read_budget` ve `verify_physical` işlemlerini sunar. Yeni pathname listener,
control socket operasyonu, scheduler, GPU tahsis otoritesi veya DB yazısı yoktur.
Private protokol önerisi `aos-scientist-retained-provider.v1`, integer version1;
mevcut public evidence-v3/evidence-v2 sözleşmeleri korunur.

Her mesajın `SCM_CREDENTIALS` PID/UID'si mevcut authenticator'a verilir; inherited
socket'in oluşturucu `SO_PEERCRED` kimliği kullanılmaz. Özgün caller generation,
target/profile/deployment ve başarılı retained discovery capSHA mevcut facade
tarafından tekrar doğrulanır. Sequence tek kullanımlıdır; client tek outstanding
istek taşır. Timeout, revoke, yanlış generation, yanlış cevap veya denial kanalı
kapatır; otomatik retry/yeniden inference yoktur. JSON ve mesajlar128KiB ile
sınırlıdır; duplicate keys, NaN, truncation ve ek ancillary data reddedilir;
gelen FD'ler kapatılır. Linux SO_PASSCRED autobind adresi kimlik sayılmaz.

Server kaynağı private policy'de pinlenmeden etkinleşmez. Fiziksel facade ayrıca
observer source pin'ini ister. Tek local en fazla3 saniyelik deadline, Store,
authenticator, systemd ve ardışık NVIDIA sorgularıyla paylaşılır; geçmiş çağrı
bütçeleri yenilenmez. Son current/deadline kontrolünden sonra cevap yayınlanır.

## AOS adaptörü ve aynı broker nesnesi

`scripts/aos_retained_provider_adapter.py` AOS interpreter'ında yalnız stdlib ve
AOS import eder. Aynı original Store/history2.0/retained host ile çalışır:

- `read_source(request, original)` candidate witness almadan broker'dan tamamını okur.
- `verify_source` özgün/current binding ve candidate yapısını doğrular; bağımsız okumanın yerini almaz.
- `verify_resolver` exact committed reconcile ACK'deki terminali current authority ile bağlar.
- `verify_physical` tam ACK evidence'ını, `result_canonical` dahil, callback preimage'larıyla karşılaştırıp broker physical facade'a gönderir.

`verify_current(operation, original, binding)` zorunlu trusted hak sağlayıcısıdır;
varsayılanı deny. Adaptör session/runtime/owner/lease/generation, original intent
ve broker generation'ını ayrıca kontrol eder. Callback AOS transaction sahipliğini
değiştirmez. AOS `verify_control` ve `verify_resolution` hakları ayrı olarak
sağlanmalıdır; bu provider journal resolution yetkisi yaratmaz.

`aos_gpu_service.serve(retained_channel=...)` explicit trusted FD bootstrap seam'i
sunar. Provider, inference ve recovery ile aynı canonical control/units/GPU
nesnelerini kullanır; ayrı bounded thread inference kapasitesini işgal etmez.
Normal CLI veya bir env flag provider endpoint oluşturmaz.

## Geçen kanıt

81 odaklı CPU kontrolü geçti. Gerçek kernel per-message credentials ile socketpair
oluşturucusundan farklı owned child PID'si doğrulandı; actual Store okumaları
öncesi/sonrası SQL dump aynı kaldı. İlk odaklı koşu6 başarısızlık yakaladı:
Linux autobind reddi ve fixture şemasının eksikliği. Autobind düzeltmesinden
sonra yalnız eksik child table kaldı; fixture actual runtime schema producer'ıyla
kurulunca81 geçti. Eksik üretim tablosu hâlâ fail-closed'dur.

Actual AOS checkout'un54 resolution kaynak pini ve ilave actual retained-host
dosya piniyle iki ayrı Python ortamı birleştirildi. AOS dosyaları read-only
mount'taydı. Existing evidence-v3 socket → immutable ACK → authenticated provider
→ independent budget/no-admission snapshot → durable resolution → fresh admission
gate, parent exit0/**3,317785366 saniye**. Original intent/history değişmedi;
eski inference replay reddedildi. Resolution transaction'ı içinde120 current
callback kontrolü yapıldı; bu sayı120 model çağrısı değildir.

## Kalan ve çalıştırılmayanlar

Bu koşuda unit/session authorization sentetik, terminal allocation öncesi iptal,
model/Scorer/GPU icrası yoktur. Parent broker nesnesi fixture'dır; gerçek canonical
servis ve ayrı yetkili AOS unit'ine FD bootstrap kurulmadı. Bu CPU bootstrap
gerçek service-generation admission yerine kullanılamaz.54+host seçilmiş kaynak
pinleri tam dependency/config runtime attestation değildir.

Bir sonraki bootstrap için yerel systemd262 belgeleri ve v262 kaynakları
incelendi: `systemd-run --user --pipe` stdin FD'sini `StandardInputFileDescriptor`
üzerinden doğrudan service'e geçirir. Socketpair'in diğer endpoint'ini stdin
olarak vermek, yeni listener olmadan AOS'ta `socket.socket(fileno=0)` kullanmayı
mümkün kılabilir. Bu henüz host üzerinde çalıştırılmış bir kabul değildir.
Kaynak zinciri: [run.c](https://github.com/systemd/systemd/blob/v262/src/run/run.c),
[service FD handling](https://github.com/systemd/systemd/blob/v262/src/core/dbus-service.c),
[exec stdin setup](https://github.com/systemd/systemd/blob/v262/src/core/exec-invoke.c).
Gerçek sender service PID'si systemd-run launcher PID'siyle karıştırılmamalı;
FD0 türü, çift yönlü credentials ve gerçek unit/process kapanışı ayrıca ölçülmeli.

Canlı bootstrap, iki tarafta gerçek current principal/controller/policy closure,
version/capability uzlaşısı ve canonical scheduler rezervasyonu tamamlanmalı.
Ardından bounded gerçek AOS görevi → GPU devri → Scientist model/deney → bağımsız
Scorer → AOS rapor doğrulama ve ayrıca kontrollü iptal/toparlanma koşusu gerekir.
VRAM, model/quantization/context ve GPU wait/devir/model gecikmeleri ölçülmedi;
CPU süreleri bunlar olarak sunulmaz. İki tarafın adil ilerlediği kabul açık.

AOS checkout'u/canlı servis/user işi/cache değiştirilmedi; push/merge/deploy yok.
Kabul toplamı **11 geçti / 7 kısmi / 4 açık**. Lisans ve genel CI ayrı kalır.

[Kaynak, sözleşme ve icra hash'leri](review-evidence/authenticated-retained-provider.json).

Zorunlu kalite kapısı **1927 passed / 7 opt-in skipped / 120 GPU-live
deselected**, yedi komut exit0; parent192,020513804 saniye, Python kaynakları
değişmedi. Paket build ve wheel import kontrolü de bu kapıya dahildir.

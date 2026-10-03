# Native R6: SQL0038 kuruldu; primary öneri uyumsuzluğu

Scientist commit `4e1bb41645bebd07338000f0c3c46d3975cdc286`, salt okunur
AOS commit `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
Scientist yerel tracked farkı boş; SHA256
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
AOS tracked fark SHA256
`ffa23487b45a093b0eea460fbc9619120f1c81b1acbf83172c78582003d1dbff`.
Dirty/untracked AOS çalışma kopyası ortak runtime kabulü değildir.

## Geçen

Yeni izole PostgreSQL'e normal dört rolle SQL0038 migration uygulandı;
kurulum, boş-run rol kontrolleri ve sentetik profil kurulumu exit0 verdi.
Yeni istek 2400 saniye / 1 deney / 24576 token rezervasyonudur.
Kaynak manifest SHA256
`f643409d8eda6fe1372584bcd4d8b59ac9dbfb3be2122de31b1852c39ec77755`;
yalnız test kopyasındaki kalibrasyon/claim gözlem kancaları kullanıldı.
Ortak Director admission kilidi aynı inode üzerinde kaldı.

Üç gerçek baseline deney, 36 bağımsız Scorer hücresi ve donmuş kalibrasyon
tamamlandı. Kontrollü G1 crash, normal recovery ile G2 sahipliği,
özgün deadline/calibration korunması ve gecikmiş G1 kapanışının reddi dahil
22 ara kontrol geçti. Bunlar sentetik CPU yaşam döngüsü kanıtlarıdır.

## Başarısız ve kalan

Genel koşu exit1; süre 1294.982582 saniye, kaynaklar değişmedi.
Primary job/claim oluşmadı; ledger `failed`, report NULL.
Bu nedenle inflight stop, SQL0038 child/final seal, üç missing-cell closure
ve terminal stopped report henüz gerçek kabul değildir.

Neden bütçe değil: durably seçilen ilk hamle `hparam`, eski senaryonun ilk
önerisi `features`. Runner öneriyi `provider changed the preselected move
intent` denetiminde reddeder. Immutable checkpoint `provider_error` ve
`ValueError` kaydeder; S1 rezervasyonu normal şekilde 18432 token olarak
konservatif ücretlendirilmiştir. Bu, gerçek model token ölçümü değildir.
Üretim stratejisi ordinal1 için her seed'de `hparam` seçer. Sonraki yeni
senaryo gerçek bir robust-z ölçek hiperparametresi değişikliği üretmeli;
başlamadan hamle eşleşmesi doğrulanmalı. Guard gevşetilmez, eski koşu
bütçesi/deadline'ı veya kayıtları değiştirilmez.

## Temiz kapanış

Bağımsız root cleanup exit0: özgün G1/G2 kimlikleri ölü, cgroup'ları boş,
tüm tarihsel Scorer işçileri fiziksel olarak kapanmış, aktif job0.
Recovery actor listesi boş; stop worker başlamadı. Fixture API ve parent
inactive/MainPID0. Yalnız kimliği doğrulanan fixture PostgreSQL durduruldu,
depolama ve failed ledger korundu. Ana servisler ve AOS değiştirilmedi.

## Çalıştırılmayan

AOS ortak sözleşme/commit/capability teyidi ve mevcut scheduler rezervasyonu
olmadan entegre GPU koşusu çalıştırılmadı. Aday sözleşme
`aos-scientist-runtime.v1 / wire1`; ortak kabul yok. Provider `fake-json`:
gerçek model/quantization yok; context rezervasyonu16384, S2 output tavanı8192.
VRAM tepe, GPU bekleme/devir/çalışma gecikmeleri ölçülmedi; release kabulü yok.
Lisans ve genel CI işleri bu yaşam döngüsü kabulünden ayrıdır.

AOS'a beklenti: teyit edilmiş sözleşme sürümü ve checkout hash'i; exact
principal/owner/generation, acquire, cancellation/status/reconcile, timeout,
drain ve trusted release/quarantine. Idle veya stop ACK release kanıtı değildir.
Entegre GPU koşusunun tek yürütücüsü Scientist oturumudur.

# Scientist → AOS kapanış ve yeniden kabul bağlantısı

2026-10-01. Scientist tabanı `62b2fb3c74a800ea75914b57ae8e9fef5a7092ed`,
AOS tabanı `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`.
Bu kayıt, note91'deki resolution API eksikliğini günceller: güncel AOS
`ScientistResolutionJournal.resolve()` ve yeni isteğe izin veren admission
anti-join'ini içeriyor. AOS checkout'u dirty/untracked; public temiz ana dal
veya çalışan deployment olarak sunulmuyor.

## Geçen bağlantı

`scripts/check_aos_resolution_composition.py` Scientist ortamında gerçek
ControlStore/LabAOSControl socket'ini açar. Ayrı AOS Python ortamındaki
`scripts/aos_resolution_composition_fixture.py` gerçek AOS admission history,
evidence client/journal, retained verifier ve resolution journal kodunu kullanır.
İptal GPU allocation öncesindedir. Yetki, saat ve fiziksel kanıt sağlayıcıları
açıkça sentetiktir; mevcut gerçek GPU tahsis otoritesi kullanılmaz/değiştirilmez.

Yürütülen zincir: original admission/intent → capability ACK → v3 reconcile
ACK → immutable journal → bağımsız retained budget doğrulama → durable
resolution → aynı store/session'da yeni request için admission. Yetki sağlayıcısı
olmayan resolution ve eski request'in yeniden kabulü reddedildi. Exact resolution
tekrarı geçti; original intent/history satırları değişmedi. Yeni inference
çalıştırılmadı; tamamlanmış model sonucu ve Scorer akışı bu koşuya dahil değil.

İlk koşu exit1: paralel AOS değişikliği expected kaynak hashlerini bozdu ve
çocuk süreç başlatılmadan preflight reddetti. Değişen seçili üyeler desktop
factory ve source profile raporuydu; mevcut doğrudan54 API korunuyor. Güncel
snapshot yeniden doğrulandı. İkinci koşu **parent exit0 / 1,389061243 saniye**;
Python kaynakları koşu boyunca sabitti. Owned child wait/exit0 ve socket thread
join parent başarı koşuludur; parent PID'nin artık bulunmadığı ayrıca okundu.

Host sınırları: CPU100%, RAM2GiB, swap0, Tasks128, Runtime600s,
CUDA_VISIBLE_DEVICES boş. AOS dizini systemd ReadOnlyPaths ile salt okunur;
bytecode yazımı kapalı. Yeni özel Scientist workdir'ındaki iki SQLite fixture
dışında DB yazılmaz. Scheduler private şeması GPU ayırmaz; resolver/drain
callback'leri bu koşuda kullanılmaları halinde hata verir. Canlı servis
restart/deploy, AOS değişikliği, model indirme ve push yapılmadı.

## Sabitlenen kaynak ve sözleşme

`resolution_candidate_v3`:54 seçili üye, runtime admission=false.

- Selected manifest SHA: `d39cad790ae4eb59eb02a9dee2ecf3f5e0f1cdeafccb9aae10a2d7d5f2dcdb92`
- Tracked binary diff SHA: `f4c4507b174ace092b849c6ffb0af77bcabd00ecfca2629362d44b36dcc7f28c`
- Selected untracked SHA: `e212cf84446597f14f4812e4ba00b99321ebedc00d916bc03602742af30eff0c`
- Evidence-v3 canonical SHA: `cbbfa1e109cf28bac8143c01975eb575b6fcfb44970d84d1c50828ca60ac1070`

Seçili kaynak pini bütün runtime bağımlılıklarının attestasyonu değildir.
İçe aktarılan gerçek dosyaların bağımsız manifesti özel koşu kaydında tutulur.
Yeni AOS retained_host55 yardımcı factory bu doğrudan54 koşuda kullanılmadı.

## Kalan ve çalıştırılmayan kabul

Gerçek GPU kabulü çalıştırılmadı. Gözlemde canonical
`swapp-lab-gpu-broker.service` load=not-found/active=inactive ve canonical
`~/.local/state/swapp-gpu/arbiter.sqlite3` yok. Doğrulanmış rezervasyon olmadan
başka GPU allocator veya canlı model koşusu başlatılmadı.

Gerekenler: canlı current authority/source/resolver/physical provider'ları,
deployment/config/dependency closure, tek canonical scheduler rezervasyonu;
ardından AOS görev → GPU devri → Scientist öneri/deney/bağımsız Scorer → AOS
rapor doğrulaması → fiziksel cleanup. Kontrollü GPU iptali ve adil birlikte
ilerleme de ayrıca açık. Bu CPU bağlantısı M0.AOS.5/7'yi kapatmaz.

Model/quantization, VRAM peak ve GPU wait/handoff/run gecikmeleri bu koşuda
ölçülmedi. Fixture context1536/output16 bir gerçek model yapılandırması değildir.
Kabul toplamları11 geçti/7 kısmi/4 açık olarak korunur. Lisans ve genel CI işleri
ayrı tutulur.

## AOS oturumuna aktarım

Scientist gerçek v3 socket'inizden gelen ACK'yi actual54 client/journal/verifier
ile immutable resolution'a ve yeni admission'a bağladı; CPU fixture zinciri
exit0. Şema-v3 pini değişmedi. Gerçek koşu için current resolution-authorizer ve
independent budget/source/physical provider'ların trusted host bağlantısı,
explicit config/dependency pinleri ve tek canonical broker gereklidir.
GPU kabulünün tek yürütücüsü Scientist oturumudur.

[Makine kaydı](review-evidence/aos-resolution-composition.json)

## Zorunlu kalite kapısı

Normal CPU ortamında yedi komut exit0; parent180,445359730 saniye,
Python kaynakları sabit. Pytest: **1802 passed / 7 opt-in skipped /
120 GPU-live deselected**. İlk ReadOnlyPaths ortamındaki kapı exit1 verdi:
user namespace kök dizin sahipliğini uid65534 gösterdiğinden sekiz private
runtime-path kontrolü haklı olarak reddetti. Güvenlik kuralı değiştirilmedi.
Bu ortam hatasının çıktısı korundu; genel kapı normal CPU runner ile geçti.

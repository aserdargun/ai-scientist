# AOS'un native bootstrap factory/provider akışı

2026-10-01. Scientist tabanı `99a0248f028140936816058b90e71f29beead10e`,
AOS `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` ve59 seçili kaynağın exact yerel pinleri.

## Geçen gerçek süreç/kayıt akışı

`check_aos_provider_service_bootstrap.py --execute --native-bootstrap` ayrı
bounded systemd servislerinde actual AOS interpreter ve Scientist broker nesnesini
birleştirdi. AOS kendi `ScientistBootstrapAdmissionFactory` ve native
`ScientistRetainedProviderClient/Adapter` sınıflarını kullandı; Scientist custom
factory/adapter bu koşuda kullanılmadı. Gerçek DesktopController/Store tarafından
session/lease binding üretildi. Runtime/profile/clock yetkisi sentetik CPU kaldı.

AOS v1 bootstrap capability öncesi kendi durable audit writer'ı exact control
frame/hash, broker peer ve original request/intent binding kaydını commit etti.
Scientist bootstrap cevabını vermeden bağımsız read-only SQLite bağlantısından
kaydı okudu; original inference intent henüz yoktu. AOS aynı Store'da prepare →
original intent/history2.0 → v3 retained capability/reconcile → native provider
→ durable resolution → fresh admission gate akışını tamamladı. Sonrasında
desktop pause/generation değişikliğiyle native factory yeni hazırlığı reddetti.

**3 control cevabı / 20 provider isteği**. Açık resolution transaction'ı içinde
120 current callback kontrolü model çağrısı değildir. Provider sırasında private
Scientist SQL dump değişmedi; original AOS intent/history korundu. İki launcher
exit0, iki unit toplandı ve iki özgün PID/start/boot generation'ının yokluğu
ayrıca doğrulandı. Parent exit0/**12,923639764 saniye**; bu GPU/model süresi değildir.

## Düzeltilen uyumluluk hatası

Salt okunur review, broker audit kontrolündeki newline varsayımını yakaladı:
AOS codec bytes'ı newline olmadan persist eder; newline yalnız wire send'de
eklenir. Broker ve audit testleri actual producer biçimine düzeltildi. Bu hata
canlı koşu başlatılmadan giderildi; testin bulduğu bir runtime hata diye sunulmaz.
122 odaklı kontrol ve düzeltme sonrası12 audit/bootstrap kontrolü geçti.

Native59 preflight exact source-selection zinciri, factory yöntemleri ve async
bootstrap/desktop API'sini doğrular. Legacy54 yolu korunur. Config hazırlayıcı
native58/59 receipt kabul eder; admission hep kapalıdır. Başarılı servis koşusu
sonrasında AOS source report değiştiği için native59 config üretim denemesi
başlamadan reddedildi; yeni plan üretilmiş sayılmaz.

## Kalan ve çalıştırılmayan

Gerçek enabled policy/profile/model/interpreter ve current principal/runtime
sağlayıcısı canonical launcher'a bağlanmalı. AOS oturumu `configured_source`
adayı/source verifier ekledi; bu daha yeni60 kapanışı bu59 icra kanıtına dahil
değildir. Sonraki bağlantı mevcut native verifier üzerinden ilerlemeli.

Canonical gerçek GPU rezervasyonu, AOS kontrollü model görevi → GPU devri →
Scientist S1/S2/deney → bağımsız Scorer → AOS rapor doğrulaması ve ayrıca
allocated cancel/recovery/fairness henüz çalıştırılmadı. Model/quantization/
context, VRAM tepesi ve GPU wait/handoff/model gecikmeleri ölçülmedi. Eğitim,
öğrenilmiş adaptör ve tamamlanmış araştırma yok. Kabul **11 geçti / 7 kısmi / 4 açık**.

Kaynak değiştiğinde eski receipt yeni koşuyu yetkilendirmez. EOF/stop ACK/idle
GPU release kanıtı değildir. Fixture private SQL scheduler şeması gerçek tahsis
otoritesi değildir; GPU acquire/drain/physical callbacks reddedildi. İki service
CPU100%/RAM1GiB/swap0/tasks128/runtime180s ile bounded ve AOS mount read-only idi.
AOS dosyaları ve başka kullanıcı/Codex süreçleri değiştirilmedi; push/merge/deploy
yok. Lisans ve genel CI ayrı tutulur.

## AOS oturumuna aktarım

Native factory durable audit + provider v1, gerçek ayrı systemd kimlikleri ve
FD0/SCM_CREDENTIALS ile çalıştı. Public control-v1 ve evidence-v3 korunuyor.
Bootstrap audit bağımsız olarak cevap öncesi okundu; çözüm transaction'ı kapanmadan
provider readback çalıştı. Yeni configured-source verifier ve real runtime
authority sabit receipt ile bağlanmalı; GPU kabulünü yalnız Scientist yürütecek.

[Commit/source/diff/contract ve cleanup kanıtları](review-evidence/native-aos-bootstrap-provider.json).

Zorunlu kalite kapısı **1997 passed / 7 opt-in skipped / 120 GPU-live deselected**;
yedi komut exit0, parent189,943378996 saniye. Python kaynakları kapı boyunca
değişmedi; wheel build ve import kontrolü geçti.

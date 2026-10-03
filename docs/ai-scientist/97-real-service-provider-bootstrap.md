# Gerçek AOS servisinden bağımsız provider bağlantısı

2026-10-01. Scientist tabanı `db344fcd5bb2cae15a0f2b616e0be1e2666d49ae`,
AOS `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` ve incelenmiş yerel farkları.

## Geçen

`scripts/check_aos_provider_service_bootstrap.py --execute` ayrı systemd user
servislerinde Scientist broker ve gerçek AOS Python ortamını çalıştırdı.
`systemd-run --pipe` aynı socketpair endpoint'ini AOS'a FD0 olarak geçirdi.
Gerçek unit/PID/start_ticks/boot/invocation/cgroup kimlikleri ve mesaj başına
SCM_CREDENTIALS doğrulandı. Socket AF_UNIX/SOCK_SEQPACKET, SO_PASSCRED=1 idi.

Existing v3 capability/reconcile → değişmez ACK → bağımsız provider okumaları →
AOS açık resolution transaction'ında durable resolution → yeni admission gate
akışı geçti. **20 provider isteği**, **2 control cevabı**;120 current callback
kontrolü model çağrısı değildir. Provider okumalarının öncesi/sonrası private
Store SQL dump eşitti. İki launcher exit0; iki unit toplandı ve her iki özgün
PID/start_ticks/boot generation'ının yokluğu ayrıca doğrulandı.
Parent exit0, **11,206059887 saniye**. Bu süre GPU/model gecikmesi değildir.

İlk gerçek koşuda 1 MiB LimitFSIZE, AOS SQLite WAL migration dosyasını engelledi.
Dosya sınırı64 MiB yapıldı; ayrı log sınırı1 MiB korunuyor. İkinci koşuda akış
geçti ancak paralel AOS geliştirmesi tracked diff'i değiştirdiği için son kaynak
kontrolü reddetti. Güncel fark hash'iyle üçüncü koşu geçti; başarısız koşular
başarılı kabul olarak sayılmadı.7 odaklı kontrol geçti.

## Kaynak ve çalışma sınırları

Çalıştırma opt-in; varsayılan yalnız kaynak planını doğrular. Canonical broker
unit ancak not-found/inactive/MainPID0 ise transient oluşturulur; mevcut unit
değiştirilmez ve driver canonical broker'ı durdurmaz. AOS unit'i özgün UUID ve
exact generation denetimiyle temizlenir. AOS checkout read-only mount'tadır.
Her servis CPU100%, RAM1 GiB, swap0, tasks128, runtime180s, start10s/stop5s,
KillMode=control-group, SendSIGKILL=yes, Restart=no ile sınırlıdır. Ortam env-i
allowlist ve loader değişkenleri UnsetEnvironment ile temizlenir.

GPU/model gözlemleri ve tahsis callbacks bu CPU koşusunda reddedilir. Private
CPU SQL fixture canonical GPU otoritesi değildir; gerçek scheduler acquisition
yapılmadı. Session/principal/profile/clock yetkisi hâlâ sentetiktir.54 seçili AOS
kaynak pini + ilave retained-host pini ve12 Scientist bootstrap dosyası tam
dependency/config attestation olarak sunulmaz. Canlı API/director, başka Codex
süreçleri ve AOS dosyaları değiştirilmedi. Push/merge/deploy yapılmadı.

## Kalan ve çalıştırılmayan

Gerçek principal/controller/config closure ve bootstrap capability'nin current
admission binding'e alınması tamamlanmalı. AOS kendi bootstrap/retained-provider
adaylarını ekledi; aynı private protokol sürümünde uyum ayrıca doğrulanmalı.
Canonical scheduler rezervasyonuyla küçük gerçek AOS görevi → GPU devri →
Scientist model/deney → bağımsız Scorer → AOS sonuç doğrulaması ve kontrollü
allocated cancellation/recovery koşusu henüz çalıştırılmadı.

Model/quantization/context, VRAM tepesi ve GPU wait/handoff/model süreleri
ölçülmedi. Eğitim ve öğrenilmiş adaptör yok. Kabul sayıları **11 geçti / 7 kısmi /
4 açık** olarak kaldı. Lisans ve genel CI işleri ayrı tutulur.

## AOS oturumuna aktarım

Private `aos-scientist-retained-provider.v1`/version1, mevcut public evidence-v3
sözleşmesini değiştirmiyor. Inherited FD0 channel gerçek ayrı unit kimlikleriyle
çalıştı; budget/physical okumaları açık resolution transaction'ına yazı eklemedi.
Mevcut host'a current yetki sağlayıcısı ve orijinal successful capability/reconcile
ACK bağları verilmeli. Stop ACK/EOF/idle GPU cleanup kanıtı değildir. Sonraki
adım gerçek bootstrap/config/current principal wiring; entegre GPU koşusunu
yalnız Scientist oturumu yürütecek.

Salt okunur güncel AOS incelemesinde `serve_desktop.main()` zaten
`scientist_admission_factory`, `scientist_bootstrap_expected_peer` ve
`scientist_confirm_runtime` parametrelerini sunuyor; desktop `prepare_infer()`
üzerinden async bootstrap çağırıyor. Yeni AOS API beklenmiyor. Factory aynı
controller Store/history2.0 üzerinde gerçek current session/lease/generation,
bağımsız expected capture doğrulaması ve durable bootstrap intent writer'ı
bağlamalı. AOS native retained provider client/adapter da mevcut.

Yeni `retained_provider_candidate_v1` kaynak profili, önceki54+host kontrolünden
daha geniştir; yeni kapanış sabit receipt ile teslim edilmeli. Native
Scientist `serve()` canonical `~/.local/state/swapp-gpu/arbiter.sqlite3` ve
`/run/user/<uid>/swapp-gpu/broker.sock` kullanır; CPU fixture private DB'si
native çalıştırma garantisi değildir. Gerçek driver önce üç profil, model ve
interpreter pinleri, enabled policy, unit eşlemeleri ve mevcut canonical
scheduler durumunu doğrulayan review edilebilir plan üretmeli.

[Hash'ler, generation kimlikleri ve kanıt](review-evidence/service-provider-bootstrap.json).

Zorunlu kalite kapısı: **1934 passed / 7 opt-in skipped / 120 GPU-live
deselected**, yedi komut exit0; parent193,848974389 saniye. Python kaynakları
kapı boyunca değişmedi; wheel build ve import kontrolü de geçti.

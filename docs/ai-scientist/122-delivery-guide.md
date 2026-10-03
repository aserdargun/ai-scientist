# 0.46 teslim kılavuzu ve bilinen eksikler

Tarih: 2026-10-03. Kullanıcı kararı: mevcut çalışan sürümü eksikleriyle teslim;
gerçek AOS içinde çalışma sonraki aşama. Yeni ürün özelliği bu teslimin koşulu değildir.

## Teslimin içeriği

Çalışan field-lab arayüzü/API/CPU Director, sentetik ve izinli veri kaynağı seçimi,
istatistikler, LSH/OPTICS/SOM mod deneyleri, NN/residual/OMR, anomali baseline'ları,
sonlu OMR akışı, deney hafızası ve saha amacı bağlamı. Kaynak, kullanım belgeleri,
testler ve kanıt özetleri teslim edilir. Özel DB, ham oturumlar, tokenlar ve model
cache'i kaynak dağıtımının parçası değildir.

Mevcut native çekirdek 0.41.1 ayrı korunur. Çalışan 0.46 kaynak dalı
`feat/omr-stream-v1`, taban commit `8e6ec0c20b21fbdc5a53dc007b884af412f0cbd3`;
çalışma ağacı değişiklikleri vardır. Ana yerel commit
`55c5300600112ab823f76ec434029d6dc23e513c`. Bu commitler yayımlanmış sürüm
commit'i olarak kullanılamaz; temiz public geçmişe teslim ayrı işlemle yapılır.

## Geçen kabul

| Kontrol | Kanıt / sonuç |
|---|---|
| Ürün kaynak kalite kapısı | 3328 passed / 7 skipped / 177 deselected; 7 komut exit 0 |
| Ön yüz | TypeScript/build; gerçek kayıt üzerinde masaüstü ve mobil okuma |
| Amaç → deney → rapor | `50ea6463-4589-4213-a2fe-817287f3a323`, 279,61 s, completed |
| Bağımsız değerlendirme | 9 baseline + 1 OPTICS primary + 2 confirmation |
| Tekrarlı başlatma | Aynı istek/key aynı run; ikinci iş yok |
| Kapanış | Director ve 12 Scorer süreç/cgroup yok, sandbox yok, kuyruk boş |
| Kalıcı kayıt | Önceki raporlar korundu, terminal rapor hash'i doğrulandı |
| Saha bağlamı | Snapshot + amaç hash'i, Director öneri bağlamına bağlanma ve terminal companion doğrulandı |

Tam [0.46 kanıtı](review-evidence/field-lab-field-intent-20261003.json).
Raporda OMR noktası sayısı 470, maske sonrası skorlanan satır 450'dir; bunlar
aynı sayı olarak sunulmaz. Bu koşuda LLM/GPU işi yoktur.

Ayrı tarihsel [yerel model araştırması](118-six-proposal-local-research.md):
altı öneri, 15 bağımsız ölçüm, 8278 token, 12890 MiB tepe VRAM; tümü DISCARD.
Bu ölçüm field-lab CPU profilinin GPU açtığı veya AOS ile ortak kabulün geçtiği
anlamına gelmez.

## Bilinen eksikler / çalıştırılmayan kabul

| Konu | Durum ve sonraki kanıt |
|---|---|
| AOS gerçek ortak GPU akışı | HOLD; native dışlama producer/consumer, source/config pinleri ve kontrollü devir gerekiyor |
| Adil GPU paylaşımı ve gerçek inflight iptal | Bu teslimde çalıştırılmadı; AOS görevi + Scientist çağrısı ilerlemesi ve fiziksel cleanup ölçülecek |
| Öğretmen eğitimi | UI yalnız taslak/hazırlık; eğitilmiş adapter yok |
| Sürekli kalıcı gelişim | Açık hafıza seçimi var; otomatik skill/model terfisi ve bağımsız gelişim ölçümü açık |
| Temiz makine kurulum | Geliştirme komutları var; tam servis/model/data bootstrap henüz başka temiz host'ta kabul edilmedi |
| Uzak Mac tarayıcı | Erişim köprüsü açık; kullanıcı cihazından pozitif tarayıcı kabulü yapılmadı |
| Tüm M0/OM/kapasite | Ayrı kabul kaydı geçerli; CPU test sayısı bu kapsamın tamamlanma yüzdesi değildir |
| Proje lisansı / genel CI | Ayrı yayın işi; lisans seçilmedi, bu teslimde tüm uzak CI garantisi verilmez |
| Gerçek ücret | Fatura yok; geçmiş log kapsamı ve varsayımsal API fiyat senaryosu ayrı |

## Mevcut kurulumun işletimi

[README başlangıç adımları](../../README.md#hazır-kurulumu-açın) günceldir.
Linux kullanıcı systemd oturumu, Docker yetkisi, Python 3.12 `.venv`, derlenmiş
`console/web/dist`, pinli PostgreSQL/sandbox imajları ve profil yapılandırması gerekir.
`--prepare` yalnız 0700 profil ve 0600 özel yapılandırma oluşturur. İlk servis
başlangıcı bu profile ait DB rollerini/migration'ları hazırlar; var olan başka
DB veya birimi devralmaz. İmajlar yoksa indirme yerine anlaşılır hata verir.

Profil varsayılan portları: console 8789, API 8767, DB 55434. İkinci profil aynı
varsayılan portlarla eşzamanlı başlatılamaz. Mevcut yapılandırma kimliği checkout
konumuna bağlıdır; klasörü taşıyıp aynı özel receipt'i kullanmayın.

- **Bağlantı yok:** `--check` ve profile ait kullanıcı servis günlüklerini inceleyin.
- **Port dolu:** sahibi belirlenmeden süreç kapatmayın; yanlış profil açılmış olabilir.
- **İmaj eksik:** pinli imajın güvenilir build/provision adımını tamamlayın; keyfi `latest` kullanmayın.
- **İş stop_requested:** terminal cleanup bekleyin. DB durumunu elle `stopped` yapmayın.
- **Quarantine:** kaynak bırakılmış varsaymayın; kimlikli cleanup kanıtı gerektirir.
- **Bütçe reddi:** RAM/disk rezervini koruyun; başka uygulamaların cache veya işlerini silmeyin.
- **Kaynak/veri yok:** izinli snapshot/registry kaydı gerekir; özel DSN'yi UI'ye yapıştırmayın.

Yedekler özel DB, blob deposu, registry ve principal yapılandırmasını kapsamalıdır.
Yedekleri public repoya koymayın. Bu teslimde restore tatbikatı yapılmadı; veri
saklama gereksinimleri olan kurulumlarda ayrı doğrulayın.

## Sürüm ve yayın kayıtları

0.46 harness hash: `fa0e0b9d5c76d49216e086a9184bf1985bc0065a3426b99b1def7c08cb892837`.
Yerel sandbox imajı: `sha256:5dfc403b6fc6e7347c80c99ca461c1902b3ca4725a9428569a33d27f0ff1214c`.
210 dosya byte eşliği doğrulandı. Bunlar taşınabilir registry pull adresi değildir.
Yayın yardımcılarının kontrolleri ve gerçek public commit/push sonucu bu teslimden
ayrı receipt ile kaydedilir; timer kurulmadan saatlik yayın aktif sayılmaz.

## Son yayın paketi kontrolü

Temiz public geçmiş üstüne seçilen 0.46 ürün kaynağı, incelenmiş native
yardımcıları ve kullanım/yayın araçlarının birleşik kontrolü: **3439 passed,
49 skipped, 177 deselected, 517 warning**. Opt-in/host bağımlı skip'ler geçmiş
bir kabul gibi sayılmaz; AOS gerçek ortak çalışma açık kalır.

İlk systemd test ortamında beş kontrol exit 0 sonrası `uv` PATH'te bulunamadı
ve komut exit 1 verdi. Kod hashleri değişmeden PATH düzeltildi; kalan wheel
build ve wheel import kontrolü exit 0 tamamlandı. Yedi gerçek kontrol sonucu
birleşik receipt'te kayıtlıdır; önceki hata gizlenmedi ve 3439 test tekrarlanmadı.
[Makine okunur teslim kaydı](review-evidence/release-046-delivery.json).

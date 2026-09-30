# AI Scientist

Yerel modellerle çalışan, uzun süreli AI/ML deneyleri için geliştirilmekte
olan bağımsız laboratuvar. Hedef; veri seçmek, yöntem ve hiperparametre
denemek, sonuçları doğrulamak ve yeniden kullanılabilir bilgi, skill ve
eğitim verisi üretmek.

**Geliştirme sürümü:** çekirdek `0.41.0`. Açık kaynak yayın hazırlığı sürüyor;
proje lisansı henüz seçilmedi. Tam M0 kabulü tamamlanmadı.

Public teslim temizlenmiş kaynak snapshot'ıyla başlar; eski yerel geliştirme
geçmişi yayımlanmaz. [Yayın ve son test durumu](docs/ai-scientist/64-automatic-stop-and-publication.md).

## Bağımsız çekirdek, farklı entegrasyonlar

Çekirdek SWAPP veya AOS gerektirmeden kurulabilir olmalıdır. Kullanıcılar
kendi fork'larında web uygulaması, ajan sistemi ve veri kaynakları için
adaptör geliştirebilir. SWAPP + AOS, özel intranette kullanılacak bir fork
senaryosudur; SWAPP şirkete özgü ve özel bir web uygulamasıdır. SWAPP kaynak
kodu, kurumsal ayarlar ve veriler genel dağıtımın parçası değildir.

Mevcut AOS entegrasyon çalışması ve aynı host'ta kaynak paylaşımı testleri
ayrı alandadır. Paket/servislerin mevcut `swapp-` adları tarihsel teknik
adlardır; genel çekirdeğin SWAPP'a bağımlı olması hedeflenmez.

## Şu anda denenebilenler

- Web arayüzünden deney başlatma, izleme, durdurma ve rapor indirme.
- Sentetik veri üretme; onaylı PostgreSQL kaynağından sensör/zaman seçimi.
- Temel istatistik, histogram, korelasyon, otokorelasyon ve veri kalitesi.
- LSH, OPTICS ve SOM ile mod belirleme; tolerans, nearest neighbor tahmini,
  sensör residual'ları ve Overall Model Residual grafikleri.
- Ayrı Docker/Scorer süreçlerinde anomali tespit ölçümleri; değişmez deney
  kayıtları, veri kökeni ve hash ile doğrulanan raporlar.

Tamamlanan örnekler: sentetik mod projesinde **12**, seçili Genesis/GECCO/
CATSv2/SMD bölümlerindeki baseline projesinde **36 gerçek ölçüm**. Bunlar
CPU deneyleridir; uzun yerel LLM araştırması veya gerçek AOS birlikte
çalışma kabulünün yerine geçmez. SMD sunucu telemetrisidir.

## Bu bilgisayarda başlatma

Mevcut CachyOS kurulumu için, normal kullanıcı terminalinde:

```bash
cd /home/cachyos/ai-scientist
bash ops/start-lab.sh
```

Tarayıcı: **http://127.0.0.1:8788**. Başlatıcı mevcut PostgreSQL, API,
deney işçisi ve arayüzü kontrol eder; kapalı bileşenleri başlatır. Açık
servisleri yeniden başlatmaz. Terminal kapatılabilir; bilgisayar yeniden
açıldığında komut tekrar çalıştırılır. Docker kapalıysa önce
`sudo systemctl start docker` çalıştırın.

Bu komut önceden hazırlanmış yerel ortamı kullanır; temiz makine kurucusu
henüz değildir. Python 3.12 ortamı (`.venv`), derlenmiş konsol, mevcut
`swapp-lab-postgres-m0` konteyneri, API/işçi ortam dosyaları ve özel
principal/suite kayıtları önceden hazırlanmış olmalıdır. Komut bu dosyaları,
veritabanını veya migration'ları oluşturmaz. Eksik yapılandırmayı boş dosya
ile tamamlamayın; mevcut kurulumun yedeğini geri yükleyin veya kurulum
sorumlusuyla giderin. Taşınabilir kurulum açık kaynak yayın planındadır.

Başlatma hata verirse konsol rehberindeki
[sorun giderme adımlarını](console/README.md#başlatma-sorununu-inceleme)
izleyin. Başlatıcı yalnız yerel üç Lab servisini ve mevcut DB'yi hazırlar;
uzaktan erişim tüneli ayrı bir özel kurulum bileşenidir. Yeniden başlatma
sonrası yerel komutun başarısı uzaktan erişimin hazır olduğunu göstermez.
Servisler kullanıcı systemd oturumunda çalışır; oturum kapandıktan sonraki
ömür kullanıcı yöneticisinin ayarlarına bağlıdır.
[İlk projeyi deneme rehberi](docs/ai-scientist/43-first-project-guide.md).

## aserdargun bilgisayarından bağlanma

Bu deponun bir kopyasında, **aserdargun bilgisayarındaki** terminalde çalıştırın.
Yalnız bağlantı için ilgili `ops/connect-lab.sh` veya `ops/connect-lab.ps1` dosyasını
aserdargun bilgisayarına kopyalamak da yeterlidir; aşağıdaki dosya yolunu buna göre değiştirin.
`CACHYOS_SSH_HOST` yerine CachyOS bilgisayarına ulaşan hostname'i veya mevcut
SSH config alias'ını yazın; varsayılan `cachyos` yalnız bu ad çözümleniyorsa
çalışır. SSH erişimi ve normal anahtar/parola doğrulaması önceden hazır olmalıdır.

Linux/macOS (Bash, SSH ve curl gerekir):

```bash
bash ops/connect-lab.sh --host CACHYOS_SSH_HOST --local-port 8878
```

Windows PowerShell (Windows OpenSSH `ssh.exe` gerekir):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\connect-lab.ps1 -HostName CACHYOS_SSH_HOST -LocalPort 8878
```

Komut CachyOS'ta mevcut Lab başlatıcısını çalıştırır, ardından yerel
`127.0.0.1:8878` portunu SSH ile CachyOS'un `127.0.0.1:8788` portuna bağlar;
Lab yanıt verdiğinde tarayıcıyı açar. Lab zaten açıksa `--no-start-lab` / `-NoStartLab`
kullanılabilir. Windows komutundaki execution policy yalnız o PowerShell süreci için geçerlidir. Terminali açık tutun; **Ctrl+C tüneli kapatır**, Lab servisleri
çalışmaya devam eder. Mevcut özel uzaktan erişim proxy'si ayrı kullanılabilir.
Port doluysa `--local-port 18788` / `-LocalPort 18788` kullanın. Kullanıcı ve
kurulum yolu `--user`, `--remote-dir` / `-SshUser`, `-RemoteDir` ile değiştirilebilir;
varsayılanlar `cachyos` ve `/home/cachyos/ai-scientist`tir. Kurulum yolu boşluk
içermemelidir. Tarayıcı açılmasını atlamak için `--no-browser` / `-NoBrowser` kullanın.

## Geliştirme süresi, token kullanımı ve modeller

2026-09-30 güncel geliştirme tercihi: **GPT-6.1 Sol / medium**. Aşağıdaki
tablo, oturum metadata’sında fiilen gözlenen model ve ayarları gösterir.

<!-- development-metrics:start -->
Sayaç güncellemesi: **2026-09-30 09:51:31 Europe/Istanbul**.

| Ölçüm | Değer |
|---|---:|
| Aktif süre | 52 saat 36 dakika 14 saniye |
| Aktif süre (saniye) | 189374 |
| Token | 89852106 |
| Takvim süresi | 142.595000 saat |

Goal aracının raporladığı sayaçlar. Faturalandırma miktarı veya insan işçiliği değildir; alt ajan/cache hesaplama kapsamı araç tarafından açıklanmıyor.

| Model | Ayar | Rol | Gözlenen oturum |
|---|---|---|---:|
| gpt-6-astra | high | Teknik orkestrasyon ve mimari inceleme | 5 |
| gpt-6-astra | xhigh | Ana Codex oturumu | 1 |
| gpt-6-luna | high | İlk uygulama ve odaklı doğrulama işleri | 7 |
| gpt-6-sol | high | Kodlama, entegrasyon ve inceleme işleri | 2 |
| gpt-6.1-sol | high | Rol doğrulanmadı | 3 |
| gpt-6.1-sol | medium | Kalan teknik orkestrasyon, uygulama ve inceleme | 12 |

Bu taramada ilişkili oturum: 29.

[Sayaç ve köken kaydı](docs/development-metrics.json).
<!-- development-metrics:end -->

Bu bölüm her anlamlı teslimatta ve son sürüm öncesinde
`scripts/update_development_metrics.py` ile yenilenir. Araç, güncel Codex
Goal snapshot'ını ve oturumların model metadatasını okur;
[ölçüm tarihçesini](docs/development-metrics-history.jsonl) saklar. Özel
snapshot ve ham oturum içerikleri bu depoya eklenmez.

Claude ile ileride yapılacak fork/entegrasyon çalışmaları bu ölçüme dahil
değildir. Ürünün yerel araştırma modeli Qwen3.5-9B geliştirme ajanlarından
ayrıdır; gerçek araştırma ve eğitim kabulündeki açıklar aşağıda izlenir.

## Sonraki teslimatlar

1. Çekirdeğin kalan kesinti/devam, izolasyon, holdout, yerel model ve kaynak
   paylaşımı kabullerini kapatmak.
2. Genel kurulum ve bağımsız adaptör sözleşmeleri; eğitim kayıt tamlığını
   bugünden doğrulamak.
3. Model kataloğu, karşılaştırmalı model/yöntem seçimi ve geri dönüş.
4. Kanıtla doğrulanan notebook ve skill üretimi/yayını.
5. Temizlenmiş SFT/KTO/DPO paketleri, ardından ölçülmüş LoRA/QLoRA adapter
   eğitimi ve bağımsız değerlendirme.

Mevcut eğitim işçisi sentetik fizibilite denemesi içindir; adapter üretimi
tamamlandı iddiası yoktur. Mevcut trajectory'ler denetim kaydıdır; otomatik
olarak temiz eğitim verisi sayılmaz. Veri, kod ve model lisansları ayrı
izlenir; mevcut deneylerin kullanım profili ticari olmayan araştırmadır.

- [Ürün mimarisi ve teslim planı](docs/ai-scientist/44-open-laboratory-roadmap.md)
- [M0 kabul ve kanıt kaydı](docs/ai-scientist/m0-acceptance.md): 22 maddenin
  11'i geçti, 7'si kısmi, 4'ü açık. Bu oran kalan iş süresinin yüzdesi değildir.
- [Çalışma modları ve OMR](docs/ai-scientist/42-operating-modes-omr-experiments.md)
- [Arayüz geliştirme rehberi](console/README.md)

## Doğrulama

Son 0.41.0 kalite kapısı: **1247 test başarılı / 7 skipped / 120 deselected**,
yedi kontrol exit 0.
[Yürütme kaydı](docs/ai-scientist/review-evidence/release041-image-gate-r4-summary.json).
0.41.0 kurulu; veritabanı yükseltme/geri alma provası ve gerçek Scorer işi geçti.
[Dağıtım kaydı](docs/ai-scientist/review-evidence/release041-deployment-summary.json).

Hazır geliştirme ortamında zorunlu kalite kapısı:

```bash
.venv/bin/python scripts/quality_gate.py
```

Kapı; statik analiz, CPU testleri, strict tip kontrolü ve wheel kontrolünü
çalıştırır. PostgreSQL/Docker, gerçek model ve AOS testleri kendi açık
çalıştırma koşulları ve kaynak sınırlarıyla ayrıca yürütülür. Başarılı CPU
kapısı tüm ürün kabullerinin tamamlandığı anlamına gelmez.

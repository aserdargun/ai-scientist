# GPU sıra paylaşımı ve native çalışma hazırlığı

Tarih: 2026-09-25. Bu dilim M0.AOS.4 için kaynak yönetimi çalışmasıdır.
Gerçek Qwen/AOS modeli, CUDA boşaltma, VRAM kapasitesi ve birlikte yük kabulü
henüz bu belgenin kanıtı değildir. Canlı AOS değiştirilmedi.

## Giderilen tasarım sorunu ve doğrulama sınırı

Önceki `gpu-aging-deadline-review.json` varsayılan kuyruk süresinde 31 AOS
çağrısına karşı Lab'a hiç sıra verilmediğini kaydeder. Yeni uygulama, çağıranın
seçtiği öncelik yerine iki sabit hizmet kimliği ve kalıcı dönüşümlü sıra kullanır.
Hizmet kimliği systemd MainPID, InvocationID, cgroup, PID başlangıç zamanı ve
boot kimliğine bağlanır. Sıra dosyası AOS/Lab uygulama veritabanlarından ayrıdır.

Model yükleme, çıkarım ve toplam çalışma süreleri ayrı son tarihlere sahiptir.
Heartbeat bu son tarihleri uzatamaz. Zaman aşımı kaynağı otomatik olarak diğer
tarafa vermez; önceki çalışma karantinada kalır ve güvenilir boşaltma gözlemi
gerektirir. Bu davranışın CPU süreçleriyle kanıtlanması, GPU belleğinin gerçekten
boşaltıldığını kanıtlamaz.

## Bağımsız süre sınırı incelemesi

`review-evidence/review_gpu_phase_bounds.py` üretim scheduler'ını özel SQLite
dosyasında, açıkça fixture olan kimlik çözücüsü ve saatle sınar. Model başlatmaz.

- İlk bağımsız çıktı `gpu-phase-bound-before.json`: çalışma izni bittikten
  sonra yinelenen `mark_ready` hâlâ başarılı yanıt verdi; gerçek exit code 1.
- Genişletilmiş çıktı `gpu-phase-bound-expanded-before.json`: yedi kontrolün
  ikisi başarısız. Biten iznin tekrarı yanında, çağıranın izin belgesindeki
  `slice_seconds` değerini değiştirmesi 30 saniyelik kayıtlı bütçeyi 600 saniyeye
  uzatabildi. Kaynak SHA `cbcb04fc…` iki incelemede de koşu boyunca sabitti.
- Çözüm, kararları kayıtlı bütçeden üretmek ve tekrar yanıtında da mevcut süre
  sınırını uygulamaktır. `gpu-phase-bound-review.json` son üretim kaynağında
  **7/7, exit 0**: değiştirilmiş izin belgesi 30 saniyelik bütçeyi uzatamaz,
  süresi dolan tekrar reddedilir, heartbeat yükleme son tarihini uzatamaz.
  Saat artık çağrı parametresi değildir; bu incelemede constructor'a açık fixture
  saat enjekte edilir. Önceki başarısız sonuçlar korunur.

`review-evidence/review_gpu_systemd_race.py` iki gerçek, geçici systemd user
servisi ve üretim kimlik çözücüsü ile **11/11, exit 0** geçti
(`gpu-systemd-race-review.json`). İki hizmet sırası onar kez çalıştı; 1–20
fencing token'larıyla sıra tam AOS/Lab dönüşümlüydü. Yirmi CPU alt sürecinin
her biri canlıyken bırakma reddedildi, önceki alt süreç bitmeden sonraki izin
verilmedi. Sıradan dış süreç iki hizmet kimliğini de kullanamadı. Yirmi bilet
`done`, aktif sahip yok; incelemenin iki geçici servisi durdu.

Her servisin gerçek sınırı 256 MiB RAM, 0,5 CPU, sıfır swap ve 16 task'tı.
Buradaki `aos` ve `lab`, kaynak yöneticisinin hizmet sıralarıdır; bu test gerçek
AOS uygulamasını veya Lab araştırma döngüsünü çalıştırmaz. Alt süreç gözlemcisi
gerçek model boşaltma gözlemcisi değildir.

Her iki son inceleme aynı scheduler SHA'sına bağlıdır:
`d24188a858c6027644cce080d8260baca6e45310dc135127afc7f7243ccd0885`.
İlk başarılı incelemeler `*-pre-gate-review.json` olarak korunur. Eski boot'a
ait aktif kayıt, zaman karşılaştırmasından önce karantinaya alınır; bu yol
hedefli testte restore edilmiş SQLite fixture'ıyla sınanır, host yeniden
başlatılmaz. Eski scheduler veritabanı otomatik dönüştürülmez veya silinmez;
yeni şema için ayrı koordinasyon dosyası gerekir.

## Host kontrolü

Salt okunur `review-evidence/review_gpu_host.py`, tek bir native model denemesi
öncesinde RAM/disk/GPU durumunu tekrar ölçer. Önerilen ilk model RAM sınırı
10 GiB, host rezervi 6 GiB, disk rezervi en az 20 GiB'dır. Bu değerler birlikte
çalışma kapasitesinin ölçüldüğü anlamına gelmez; cgroup uygulaması ve tüm
bileşenlerin toplam bütçesi runtime diliminde ayrıca zorlanmalıdır.

İlk kayıtta (`gpu-native-host-preflight-before.json`, exit 1) GPU'da yaklaşık
3,6 GiB kullanan, incelemenin başlatmadığı Python PID 2186855 vardı. Hiçbir dış
süreç durdurulmadı. İlk yardımcı kod ayrıca login ekranının KWin sürecinin
`/proc/PID/exe` dosyasını okuyamadığı için onu muaf tutmamıştı. Yardımcı kod,
`plasmalogin` hesabı ve tam `plasma-login-kwin_wayland.service` cgroup kimliğini
ayrı ve dar bir masaüstü istisnası olarak tanıyacak şekilde düzeltildi.

Sonraki `gpu-native-host-preflight.json` ölçümünde dış Python süreci kendiliğinden
bitmişti; dört kontrol geçti, exit 0. GPU 62 MiB, kullanılabilir RAM yaklaşık
21,9 GiB, boş disk yaklaşık 76,9 GiB idi. Swap kapasiteye eklenmedi. Bu iki
gözlem, boş GPU kontrolünün tek başına atomik kaynak rezervasyonu olmadığını
gösterir. Model başlatmadan hemen önce yeniden kontrol ve çalışma boyunca
katılmayan tüketici gözlemi gerekir. KWin veya diğer uygulamalar durdurulmaz.

## İmaj ve kalite kapısı

Harness sürümü **0.15.0**. `gpu-arbitration-image-review.json` kaynakları
dondurulmuş bir build context'inden ağsız imaj oluşturma ve konteynerde dosya
hash eşliğini doğrular; build ve parity exit 0. Güncel imaj
`sha256:f02913da232cea8c6c2c108c5ea9da2d6f4802780674b8510ecc118a465d0fdc`,
harness fingerprint `6936fdaea0a194a2b8e7d61184f46446f4acacda6246a5b112a1f57a96e59d3d`.

İlk tam kalite kapısı yalnız Bandit nedeniyle exit 1 verdi: sabit systemctl
çağrısının subprocess importu için gerekçe ve üretimdeki `assert` kontrolü.
Dar import gerekçesi eklendi; kontrol explicit RuntimeError oldu. Son kaynakla
imaj ve her iki bağımsız inceleme tekrar doğrulandı; **tam gate yeniden koşuldu**:

- Ruff, Pylint (9.30/10), Bandit, wheel build/import: exit 0.
- Pytest: **177 passed, 11 deselected**, exit 0.
- Strict mypy: **63 kaynak**, exit 0.
- Yedi alt komut ve systemd içindeki gate süreci: **exit 0**.

Değişmez gate: `evidence/quality-gate-gpu-arbitration.json`. İlk başarısız
gate `evidence/quality-gate-gpu-arbitration-before.json` dosyasındadır.
Komut ve log konumu `review-evidence/gpu-arbitration-quality-command.json`,
nihai kaynak/imaj/gate/inceleme hash bağı
`review-evidence/gpu-arbitration-quality-gate-binding.json` içindedir.

Tekrar üretim komutları (yeni çıktı dosya adları kullanılır):

```sh
.venv/bin/python docs/ai-scientist/review-evidence/review_gpu_phase_bounds.py --output /tmp/gpu-phase-review.json
.venv/bin/python docs/ai-scientist/review-evidence/review_gpu_systemd_race.py --output /tmp/gpu-systemd-review.json
.venv/bin/python scripts/quality_gate.py
```

Kalite komutunda PATH hem `.venv/bin` hem `/home/cachyos/.local/bin` içermelidir;
son kayıtta kullanılan systemd komutu bu yolu ve 3 GiB/2 CPU/sıfır swap
sınırlarını açıkça içerir. Bu gate gerçek model kabulü değildir.

## Açık işler

- Kimlikli native model süreçlerini başlatma/durdurma, cgroup boşluğu, CUDA
  süreçlerinin kaybolması ve VRAM geri kazanımı.
- AOS Decider/Bonsai/vision ve Lab yerel LLM çağrılarının ortak protokole
  bağlanması; AOS değişiklikleri yeni izole test kopyasında hazırlanır.
- Gerçek model açılış/çağrı/geçiş süreleri, S1/S2, 32k × 2 kapasite, eğitim
  ölçümü ve toplam CPU/RAM/disk bütçeleri.
- Gerçek AOS etkileşimli görevi ile Lab araştırmasının beraber ilerlemesi.

M0.AOS.4–7 ve M0.13–14, bu hazırlık belgesi nedeniyle tamamlanmış sayılmaz.

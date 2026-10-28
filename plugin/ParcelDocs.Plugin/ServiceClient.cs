using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading.Tasks;

namespace ParcelDocs.Plugin
{
    /// <summary>Client for the local ParcelDocs service (PRD §5.3). No CAD types here.</summary>
    public sealed class ServiceClient : IDisposable
    {
        // 127.0.0.1 rather than "localhost": the service binds IPv4 only.
        public const string BaseUrl = "http://127.0.0.1:8765";

        private readonly HttpClient _http;

        public ServiceClient()
        {
            // Resolving waits for the sources' rate limits, so it can take minutes.
            _http = new HttpClient { BaseAddress = new Uri(BaseUrl), Timeout = TimeSpan.FromMinutes(15) };
        }

        public bool IsRunning()
        {
            try
            {
                var response = Run(() => _http.GetAsync("/health"));
                return response.IsSuccessStatusCode;
            }
            catch (HttpRequestException)
            {
                return false;
            }
            catch (TaskCanceledException)
            {
                return false;
            }
        }

        public ResolveResponse Resolve(IList<double[]> vertices)
        {
            return Post<ResolveRequest, ResolveResponse>(
                "/resolve", new ResolveRequest { Vertices = vertices, Crs = "EPSG:2039" });
        }

        public DownloadResponse Download(string resolveId, string project, IList<string> selected)
        {
            return Post<DownloadRequest, DownloadResponse>(
                "/download",
                new DownloadRequest { ResolveId = resolveId, Project = project, Selected = selected });
        }

        public HealthResponse Health()
        {
            return Get<HealthResponse>("/health");
        }

        // The mavat step: the service opens plan pages in the browser and files what the
        // user downloads there. Every status call also checks the downloads folder.
        public AssistStatusDto MavatStart(string folder)
        {
            return Post<MavatStartRequest, AssistStatusDto>("/mavat/start", new MavatStartRequest { Folder = folder });
        }

        public AssistStatusDto MavatStatus()
        {
            return Get<AssistStatusDto>("/mavat/status");
        }

        public AssistStatusDto MavatOpen(string planId)
        {
            return Post<MavatPlanRequest, AssistStatusDto>("/mavat/open", new MavatPlanRequest { PlanId = planId });
        }

        public AssistStatusDto MavatAssign(string name, string planId)
        {
            return Post<MavatPlanRequest, AssistStatusDto>(
                "/mavat/assign", new MavatPlanRequest { Name = name, PlanId = planId });
        }

        public AssistStatusDto MavatStop()
        {
            return Post<MavatPlanRequest, AssistStatusDto>("/mavat/stop", new MavatPlanRequest());
        }

        private TResponse Get<TResponse>(string path)
        {
            var response = Run(() => _http.GetAsync(path));
            return Read<TResponse>(response);
        }

        private TResponse Post<TRequest, TResponse>(string path, TRequest body)
        {
            var content = new StringContent(Serialize(body), Encoding.UTF8, "application/json");
            var response = Run(() => _http.PostAsync(path, content));
            return Read<TResponse>(response);
        }

        private static TResponse Read<TResponse>(HttpResponseMessage response)
        {
            var text = Run(() => response.Content.ReadAsStringAsync());
            if (!response.IsSuccessStatusCode)
            {
                throw new ServiceException(ErrorDetail(text) ?? $"HTTP {(int)response.StatusCode}");
            }
            return Deserialize<TResponse>(text);
        }

        // Run async calls off AutoCAD's UI synchronization context to avoid deadlocks.
        private static T Run<T>(Func<Task<T>> call)
        {
            return Task.Run(call).GetAwaiter().GetResult();
        }

        private static string Serialize<T>(T value)
        {
            var serializer = new DataContractJsonSerializer(typeof(T));
            using (var stream = new MemoryStream())
            {
                serializer.WriteObject(stream, value);
                return Encoding.UTF8.GetString(stream.ToArray());
            }
        }

        private static T Deserialize<T>(string json)
        {
            var serializer = new DataContractJsonSerializer(typeof(T));
            using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(json)))
            {
                return (T)serializer.ReadObject(stream);
            }
        }

        private static string ErrorDetail(string json)
        {
            try
            {
                return Deserialize<ErrorResponse>(json).Detail;
            }
            catch (Exception ex) when (ex is SerializationException || ex is InvalidCastException)
            {
                // Validation errors (422) carry a list of objects instead of a string.
                return json.Length > 300 ? json.Substring(0, 300) : json;
            }
        }

        public void Dispose()
        {
            _http.Dispose();
        }
    }

    public sealed class ServiceException : Exception
    {
        public ServiceException(string message) : base(message) { }
    }

    [DataContract]
    public sealed class ResolveRequest
    {
        [DataMember(Name = "vertices")] public IList<double[]> Vertices { get; set; }
        [DataMember(Name = "crs")] public string Crs { get; set; }
    }

    [DataContract]
    public sealed class DownloadRequest
    {
        [DataMember(Name = "resolve_id")] public string ResolveId { get; set; }
        [DataMember(Name = "project")] public string Project { get; set; }
        [DataMember(Name = "selected")] public IList<string> Selected { get; set; }
    }

    [DataContract]
    public sealed class ErrorResponse
    {
        // FastAPI validation errors carry a list here; only string details are shown.
        [DataMember(Name = "detail")] public string Detail { get; set; }
    }

    [DataContract]
    public sealed class AddressDto
    {
        [DataMember(Name = "street")] public string Street { get; set; }
        [DataMember(Name = "number")] public string Number { get; set; }
    }

    [DataContract]
    public sealed class ParcelDto
    {
        [DataMember(Name = "gush")] public int Gush { get; set; }
        [DataMember(Name = "helka")] public int Helka { get; set; }
        [DataMember(Name = "status")] public string Status { get; set; }
        [DataMember(Name = "addresses")] public List<AddressDto> Addresses { get; set; }
    }

    [DataContract]
    public sealed class PlanDto
    {
        [DataMember(Name = "id")] public string Id { get; set; }
        [DataMember(Name = "number")] public string Number { get; set; }
        [DataMember(Name = "name")] public string Name { get; set; }
        [DataMember(Name = "plan_type")] public string PlanType { get; set; }
        [DataMember(Name = "approval_date")] public string ApprovalDate { get; set; }
        [DataMember(Name = "documents")] public int Documents { get; set; }
        [DataMember(Name = "documents_source")] public string DocumentsSource { get; set; }
        [DataMember(Name = "manual_action")] public string ManualAction { get; set; }
        [DataMember(Name = "mavat_url")] public string MavatUrl { get; set; }
    }

    [DataContract]
    public sealed class PermitDto
    {
        [DataMember(Name = "id")] public string Id { get; set; }
        [DataMember(Name = "permit_number")] public string PermitNumber { get; set; }
        [DataMember(Name = "request_number")] public string RequestNumber { get; set; }
        [DataMember(Name = "date")] public string Date { get; set; }
        [DataMember(Name = "description")] public string Description { get; set; }
        [DataMember(Name = "documents")] public int Documents { get; set; }
        [DataMember(Name = "manual_action")] public string ManualAction { get; set; }
    }

    [DataContract]
    public sealed class ResolveResponse
    {
        [DataMember(Name = "resolve_id")] public string ResolveId { get; set; }
        [DataMember(Name = "parcels")] public List<ParcelDto> Parcels { get; set; }
        [DataMember(Name = "plans")] public List<PlanDto> Plans { get; set; }
        [DataMember(Name = "permits")] public List<PermitDto> Permits { get; set; }
        [DataMember(Name = "warnings")] public List<string> Warnings { get; set; }
    }

    [DataContract]
    public sealed class DownloadResponse
    {
        [DataMember(Name = "folder")] public string Folder { get; set; }
        [DataMember(Name = "files")] public int Files { get; set; }
        [DataMember(Name = "from_cache")] public int FromCache { get; set; }
        [DataMember(Name = "plans")] public int Plans { get; set; }
        [DataMember(Name = "permits")] public int Permits { get; set; }
        [DataMember(Name = "warnings")] public List<string> Warnings { get; set; }
        [DataMember(Name = "mavat_pending")] public int MavatPending { get; set; }
    }

    [DataContract]
    public sealed class HealthResponse
    {
        [DataMember(Name = "status")] public string Status { get; set; }
        [DataMember(Name = "output_dir")] public string OutputDir { get; set; }
    }

    [DataContract]
    public sealed class MavatStartRequest
    {
        [DataMember(Name = "folder")] public string Folder { get; set; }
    }

    [DataContract]
    public sealed class MavatPlanRequest
    {
        [DataMember(Name = "plan_id", EmitDefaultValue = false)] public string PlanId { get; set; }
        [DataMember(Name = "name", EmitDefaultValue = false)] public string Name { get; set; }
    }

    [DataContract]
    public sealed class AssistPlanDto
    {
        [DataMember(Name = "id")] public string Id { get; set; }
        [DataMember(Name = "number")] public string Number { get; set; }
        [DataMember(Name = "name")] public string Name { get; set; }
        [DataMember(Name = "mavat_url")] public string MavatUrl { get; set; }
        [DataMember(Name = "has_instructions")] public bool HasInstructions { get; set; }
        [DataMember(Name = "has_map")] public bool HasMap { get; set; }
        [DataMember(Name = "done")] public bool Done { get; set; }
        [DataMember(Name = "files")] public List<string> Files { get; set; }
    }

    [DataContract]
    public sealed class FiledFileDto
    {
        [DataMember(Name = "name")] public string Name { get; set; }
        [DataMember(Name = "plan_id")] public string PlanId { get; set; }
        [DataMember(Name = "plan_number")] public string PlanNumber { get; set; }
        [DataMember(Name = "kind")] public string Kind { get; set; }
        [DataMember(Name = "path")] public string Path { get; set; }
        [DataMember(Name = "reason")] public string Reason { get; set; }
        [DataMember(Name = "duplicate")] public bool Duplicate { get; set; }
        [DataMember(Name = "in_library")] public bool InLibrary { get; set; }
    }

    [DataContract]
    public sealed class UnidentifiedFileDto
    {
        [DataMember(Name = "name")] public string Name { get; set; }
        [DataMember(Name = "reason")] public string Reason { get; set; }
    }

    [DataContract]
    public sealed class AssistStatusDto
    {
        [DataMember(Name = "folder")] public string Folder { get; set; }
        [DataMember(Name = "downloads_dir")] public string DownloadsDir { get; set; }
        [DataMember(Name = "active")] public string Active { get; set; }
        [DataMember(Name = "plans")] public List<AssistPlanDto> Plans { get; set; }
        [DataMember(Name = "filed")] public List<FiledFileDto> Filed { get; set; }
        [DataMember(Name = "unidentified")] public List<UnidentifiedFileDto> Unidentified { get; set; }
    }
}
